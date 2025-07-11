# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
# Modified by Suchen for HOI detection
import torch
import torch.nn.functional as F
from torch import nn
from utils import box_ops
from utils.misc import accuracy, get_world_size, is_dist_avail_and_initialized


class SetCriterion(nn.Module):
    """ This class computes the loss for DETR.
    The process happens in two steps:
        1) we compute hungarian assignment between ground truth boxes and the outputs of the model
        2) we supervise each pair of matched ground-truth / prediction (supervise class and box)
    """
    def __init__(self, matcher, weight_dict, eos_coef, losses, enable_focal_loss=False, focal_alpha=0.5, focal_gamma=0.2, consider_all=False):
        """ Create the criterion.
        Parameters:
            matcher: module able to compute a matching between targets and proposals
            weight_dict: dict containing as key the names of the losses and as values their relative weight.
            eos_coef: relative classification weight applied to the no-object category
            losses: list of all the losses to be applied. See get_loss for list of available losses.
        """
        super().__init__()
        self.matcher = matcher
        self.weight_dict = weight_dict
        self.eos_coef = eos_coef
        self.losses = losses
        self.enable_focal_loss = enable_focal_loss
        self.focal_alpha = focal_alpha
        self.focal_gamma = focal_gamma
        self.consider_all = consider_all
    
    def binary_focal_loss_with_logits(
        self,
        x: torch.Tensor, y: torch.Tensor,
        alpha: float = 0.5,
        gamma: float = 2.0,
        reduction: str = 'mean',
        eps: float = 1e-6
    ) -> torch.Tensor:
        """
        Focal loss by Lin et al.
        https://arxiv.org/pdf/1708.02002.pdf

        L = - |1-y-alpha| * |y-x|^{gamma} * log(|1-y-x|)

        Parameters:
        -----------
        x: Tensor[N, K]
            Post-normalisation scores
        y: Tensor[N, K]
            Binary labels
        alpha: float
            Hyper-parameter that balances between postive and negative examples
        gamma: float
            Hyper-paramter suppresses well-classified examples
        reduction: str
            Reduction methods
        eps: float
            A small constant to avoid NaN values from 'PowBackward'

        Returns:
        --------
        loss: Tensor
            Computed loss tensor
        """
        loss = (1 - y - alpha).abs() * ((y-torch.sigmoid(x)).abs() + eps) ** gamma * \
            torch.nn.functional.binary_cross_entropy_with_logits(
                x, y, reduction='none'
            )
        if reduction == 'mean':
            return loss.mean()
        elif reduction == 'sum':
            return loss.sum()
        elif reduction == 'none':
            return loss
        else:
            raise ValueError("Unsupported reduction method {}".format(reduction))
    
    def loss_labels(self, outputs, targets, indices, num_boxes, log=True):
        """Classification loss (NLL)
        targets dicts must contain the key "labels" containing a tensor of dim [nb_target_boxes]
        """
        assert 'logits_per_hoi' in outputs
        src_logits = outputs['logits_per_hoi']
        target_classes_i, target_classes_t = self._get_tgt_labels(targets, indices, src_logits.device)
        idx = self._get_src_permutation_idx(indices)
        # focal loss
        if self.enable_focal_loss:
            labels = torch.zeros_like(src_logits[idx], device=src_logits.device)
            labels[torch.arange(src_logits[idx].shape[0]).to(src_logits.device), target_classes_i] = 1
            focal_loss = self.binary_focal_loss_with_logits(src_logits[idx], labels, reduction='sum', alpha=self.focal_alpha, gamma=self.focal_gamma)
            focal_loss = focal_loss / len(labels)
            losses = {'loss_ce': focal_loss}
        else:
            # image-to-text alignment loss
            loss_i = F.cross_entropy(src_logits[idx], target_classes_i)
            # text-to-image alignment loss
            if self.training:
                num_tgts = target_classes_t.shape[1]
                loss_t = self.masked_out_cross_entropy(src_logits[idx][:, :num_tgts].t(), target_classes_t.t())
                losses = {"loss_ce": (loss_i + loss_t) / 2}
            else:
                losses = {'loss_ce': loss_i}

        if log:
            # TODO this should probably be a separate loss, not hacked in this one here
            losses['class_error'] = 100 - accuracy(src_logits[idx], target_classes_i)[0]
        return losses

    def masked_out_cross_entropy(self, src_logits, target_classes):
        loss = 0
        num_pos = target_classes.sum(dim=-1)
        # If there is only one active positive label, then this will be ordinary cross entropy
        indices = torch.nonzero(num_pos < 2, as_tuple=True)[0]
        targets_one_pos = torch.argmax(target_classes[indices], dim=-1)
        loss += F.cross_entropy(src_logits[indices], targets_one_pos, reduction="sum")

        # If there are multiple positive labels, then we compute them one by one. Each time,
        # the other positive labels are masked out.
        indices = torch.nonzero(num_pos > 1, as_tuple=True)[0]
        for i in indices:
            t = target_classes[i]
            cnt = sum(t)
            loss_t = 0
            for j in torch.nonzero(t):
                mask = (t == 0)
                mask[j] = True
                tgt = t[mask].argmax(dim=-1, keepdim=True)
                loss_t += F.cross_entropy(src_logits[i:i+1, mask], tgt, reduction="sum")
            loss += (loss_t / cnt)
        loss = loss / len(src_logits)
        return loss

    def loss_confidences(self, outputs, targets, indices, num_boxes, log=True):
        """ Bounding box confidence score for the interaction prediction. """
        assert 'box_scores' in outputs
        box_scores = outputs['box_scores'].sigmoid()

        idx = self._get_src_permutation_idx(indices)
        target_classes_o = torch.cat([torch.ones(len(J), dtype=torch.int64, device=box_scores.device) for t, (_, J) in zip(targets, indices)])
        target_classes = torch.full(box_scores.shape[:2], 0, dtype=torch.int64, device=box_scores.device)
        target_classes[idx] = target_classes_o
        target_classes = target_classes.to(box_scores.dtype)

        weight = torch.ones_like(target_classes) * self.eos_coef
        weight[idx] = 1.
        loss_conf = F.binary_cross_entropy(box_scores.flatten(), target_classes.flatten(), weight=weight.flatten())
        losses = {'loss_conf': loss_conf}
        return losses

    def loss_boxes(self, outputs, targets, indices, num_boxes, log=False):
        """Compute the losses related to the bounding boxes, the L1 regression loss and the GIoU loss
           targets dicts must contain the key "boxes" containing a tensor of dim [nb_target_boxes, 4]
           The target boxes are expected in format (center_x, center_y, w, h), normalized by the image size.
        """
        assert 'pred_boxes' in outputs
        idx = self._get_src_permutation_idx(indices)
        src_boxes = outputs['pred_boxes'][idx]
        target_boxes = []
        for t, (_, indices_per_t) in zip(targets, indices):
            for i in indices_per_t:
                person_id = t["hois"][i]["subject_id"]
                object_id = t["hois"][i]["object_id"]
                target_boxes.append(torch.cat([t["boxes"][person_id], t["boxes"][object_id]]))
        target_boxes = torch.stack(target_boxes, dim=0)

        loss_pbbox = F.l1_loss(src_boxes[:, :4], target_boxes[:, :4], reduction='none')
        loss_obbox = F.l1_loss(src_boxes[:, 4:], target_boxes[:, 4:], reduction='none')

        losses = {}
        losses['loss_bbox'] = loss_pbbox.sum() / num_boxes + loss_obbox.sum() / num_boxes

        loss_pgiou = 1 - torch.diag(box_ops.generalized_box_iou(
            box_ops.box_cxcywh_to_xyxy(src_boxes[:, :4]),
            box_ops.box_cxcywh_to_xyxy(target_boxes[:, :4])))
        loss_ogiou = 1 - torch.diag(box_ops.generalized_box_iou(
            box_ops.box_cxcywh_to_xyxy(src_boxes[:, 4:]),
            box_ops.box_cxcywh_to_xyxy(target_boxes[:, 4:])))

        losses['loss_giou'] = loss_pgiou.sum() / num_boxes + loss_ogiou.sum() / num_boxes
        return losses

    def loss_masks(self, outputs, targets, indices, num_boxes, log=False):
        """Compute the losses related to the mask regions using binary classification loss.
        targets dicts must contain the key "mask_region_hw" containing a tensor of dim [nb_target_boxes, H, W].
        """
        assert 'hum_region' in outputs and 'obj_region' in outputs and 'uni_region' in outputs
        idx = self._get_src_permutation_idx(indices)

        # 获取人和物体的输出mask
        pred_hum_region = outputs['hum_region'][idx[0]]
        pred_obj_region = outputs['obj_region'][idx[0]]
        pred_uni_region = outputs['uni_region'][idx[0]]

        # 收集目标的mask
        target_hum_masks = []
        target_obj_masks = []
        
        for t, (_, indices_per_t) in zip(targets, indices):
            for i in indices_per_t:
                person_id = t["hois"][i]["subject_id"]
                object_id = t["hois"][i]["object_id"]
                target_hum_masks.append(t["mask_region_hw"][person_id])  # 取人mask
                target_obj_masks.append(t["mask_region_hw"][object_id])  # 取物体mask

        # 将目标mask堆叠为张量
        target_hum_masks = torch.stack(target_hum_masks, dim=0)
        target_obj_masks = torch.stack(target_obj_masks, dim=0)

        # 生成目标统一mask
        target_uni_mask = torch.max(target_hum_masks, target_obj_masks)  # 取最大值作为统一mask

        # 计算人和物体mask的损失
        loss_hum_mask = F.binary_cross_entropy_with_logits(pred_hum_region, target_hum_masks, reduction='mean')
        loss_obj_mask = F.binary_cross_entropy_with_logits(pred_obj_region, target_obj_masks, reduction='mean')

        # 计算统一mask的损失
        loss_uni_mask = F.binary_cross_entropy_with_logits(pred_uni_region, target_uni_mask, reduction='mean')

        # 计算总损失
        losses = {}
        losses['loss_hum_mask'] = loss_hum_mask
        losses['loss_obj_mask'] = loss_obj_mask
        losses['loss_uni_mask'] = loss_uni_mask

        return losses


    def _get_src_permutation_idx(self, indices):
        # permute predictions following indices
        batch_idx = torch.cat([torch.full_like(src, i) for i, (src, _) in enumerate(indices)])
        src_idx = torch.cat([src for (src, _) in indices])
        return batch_idx, src_idx

    def _get_tgt_permutation_idx(self, indices):
        # permute targets following indices
        batch_idx = torch.cat([torch.full_like(tgt, i) for i, (_, tgt) in enumerate(indices)])
        tgt_idx = torch.cat([tgt for (_, tgt) in indices])
        return batch_idx, tgt_idx

    def _get_tgt_labels(self, targets, indices, device):
        if self.training and not self.consider_all:
            unique_hois, cnt = {}, 0 # Get unique hoi ids in the mini-batch
            for t in targets:
                for hoi in t["hois"]:
                    hoi_id = hoi["hoi_id"]
                    if hoi_id not in unique_hois:
                        unique_hois[hoi_id] = cnt
                        cnt += 1
            target_classes_i = []
            for t, (_, indices_per_t) in zip(targets, indices):
                for i in indices_per_t:
                    hoi_id = t["hois"][i]["hoi_id"]
                    target_classes_i.append(unique_hois[hoi_id])

            num_fgs = len(torch.cat([src for (src, _) in indices]))
            target_classes_t = torch.zeros((num_fgs, cnt), dtype=torch.int64)
            for i, cls_id in zip(range(len(target_classes_i)), target_classes_i):
                target_classes_t[i, cls_id] = 1
            target_classes_t = target_classes_t.to(device)
        else: ## Consider all HOIs
            target_classes_i = []
            for t, (_, indices_per_t) in zip(targets, indices):
                for i in indices_per_t:
                    target_classes_i.append(t["hois"][int(i)]["hoi_id"])
            target_classes_t = None # Skip the calculation of text-to-image alignment at inference
        target_classes_i = torch.as_tensor(target_classes_i).to(device)
        return target_classes_i, target_classes_t

    def get_loss(self, loss, outputs, targets, indices, num_boxes, **kwargs):
        loss_map = {
            'labels': self.loss_labels,
            'boxes': self.loss_boxes,
            "confidences": self.loss_confidences,
            'masks': self.loss_masks,
        }
        assert loss in loss_map, f'do you really want to compute {loss} loss?'
        return loss_map[loss](outputs, targets, indices, num_boxes, **kwargs)

    def forward(self, outputs, targets):
        """ This performs the loss computation.
        Parameters:
             outputs: dict of tensors, see the output specification of the model for the format
             targets: list of dicts, such that len(targets) == batch_size.
                      The expected keys in each dict depends on the losses applied, see each loss' doc
        """
        # Retrieve the matching between the outputs of the last layer and the targets
        indices = self.matcher(outputs, targets)

        # Compute the average number of target boxes accross all nodes, for normalization purposes
        num_boxes = sum(len(t["hois"]) for t in targets)
        num_boxes = torch.as_tensor([num_boxes], dtype=torch.float, device=next(iter(outputs.values())).device)
        if is_dist_avail_and_initialized():
            torch.distributed.all_reduce(num_boxes)
        num_boxes = torch.clamp(num_boxes / get_world_size(), min=1).item()

        # Compute all the requested losses
        losses = {}
        for loss in self.losses:
            losses.update(self.get_loss(loss, outputs, targets, indices, num_boxes))

        if 'aux_outputs' in outputs:
            for i, aux_outputs in enumerate(outputs['aux_outputs']):
                aux_outputs.update({'logits_per_hoi': outputs['logits_per_hoi']})
                indices = self.matcher(aux_outputs, targets)
                for loss in ['boxes', 'confidences']:
                    l_dict = self.get_loss(loss, aux_outputs, targets, indices, num_boxes)
                    l_dict = {k + f'_{i}': v for k, v in l_dict.items()}
                    losses.update(l_dict)

        return losses, indices