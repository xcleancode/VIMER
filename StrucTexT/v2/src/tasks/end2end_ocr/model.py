""" DB """
import os
import sys
import math
import json
import copy
import paddle as P
import numpy as np
import paddle
from paddle import nn
from paddle.nn import functional as F
from paddle import ParamAttr

__dir__ = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.abspath(os.path.join(__dir__, '../../..')))

from StrucTexT.arch.base_model import Encoder
from StrucTexT.backbones.resnet_vd import ConvBNLayer
from tasks.text_spotting_db.recg_head import RecgHead
from tasks.text_spotting_db.dataset import LabelConverter

from postprocess.db_postprocess import DBPostProcess
from paddle.vision.ops import roi_align

class BalanceLoss(nn.Layer):
    """The BalanceLoss for Differentiable Binarization text detection
    args:
        balance_loss (bool): whether balance loss or not, default is True
        main_loss_type (str): can only be one of ['CrossEntropy','DiceLoss',
            'Euclidean','BCELoss', 'MaskL1Loss'], default is  'DiceLoss'.
        negative_ratio (int|float): float, default is 3.
        return_origin (bool): whether return unbalanced loss or not, default is False.
        eps (float): default is 1e-6.
    """
    def __init__(self,
                 balance_loss=True,
                 main_loss_type='DiceLoss',
                 negative_ratio=3,
                 return_origin=False,
                 eps=1e-6,
                 **kwargs):
  
        super(BalanceLoss, self).__init__()
        self.balance_loss = balance_loss
        self.main_loss_type = main_loss_type
        self.negative_ratio = negative_ratio
        self.return_origin = return_origin
        self.eps = eps

        if self.main_loss_type == "CrossEntropy":
            self.loss = nn.CrossEntropyLoss()
        elif self.main_loss_type == "Euclidean":
            self.loss = nn.MSELoss()
        elif self.main_loss_type == "DiceLoss":
            self.loss = DiceLoss(self.eps)
        elif self.main_loss_type == "BCELoss":
            self.loss = BCELoss(reduction='none')
        elif self.main_loss_type == "MaskL1Loss":
            self.loss = MaskL1Loss(self.eps)
        else:
            loss_type = [
                'CrossEntropy', 'DiceLoss', 'Euclidean', 'BCELoss', 'MaskL1Loss'
            ]
            raise Exception(
                "main_loss_type in BalanceLoss() can only be one of {}".format(
                    loss_type))

    def forward(self, pred, gt, mask=None):
        """
        The BalanceLoss for Differentiable Binarization text detection
        args:
            pred (variable): predicted feature maps.
            gt (variable): ground truth feature maps.
            mask (variable): masked maps.
        return: (variable) balanced loss
        """
        positive = gt * mask
        negative = (1 - gt) * mask

        positive_count = int(positive.sum())
        negative_count = int(
            min(negative.sum(), positive_count * self.negative_ratio))
        loss = self.loss(pred, gt, mask=mask)

        if not self.balance_loss:
            return loss

        positive_loss = positive * loss
        negative_loss = negative * loss
        negative_loss = paddle.reshape(negative_loss, shape=[-1])
        if negative_count > 0:
            sort_loss = negative_loss.sort(descending=True)
            negative_loss = sort_loss[:negative_count]
            # negative_loss, _ = paddle.topk(negative_loss, k=negative_count_int)
            balance_loss = (positive_loss.sum() + negative_loss.sum()) / (
                positive_count + negative_count + self.eps)
        else:
            balance_loss = positive_loss.sum() / (positive_count + self.eps)
        if self.return_origin:
            return balance_loss, loss

        return balance_loss


class DiceLoss(nn.Layer):
    """DiceLoss function.
    """
    def __init__(self, eps=1e-6):
        super(DiceLoss, self).__init__()
        self.eps = eps

    def forward(self, pred, gt, mask, weights=None):
        """forward
        """
        assert pred.shape == gt.shape
        assert pred.shape == mask.shape
        if weights is not None:
            assert weights.shape == mask.shape
            mask = weights * mask
        intersection = paddle.sum(pred * gt * mask)

        union = paddle.sum(pred * mask) + paddle.sum(gt * mask) + self.eps
        loss = 1 - 2.0 * intersection / union
        assert loss <= 1
        return loss


class BCELoss(nn.Layer):
    """BCEloss
    """
    def __init__(self, reduction='mean'):
        super(BCELoss, self).__init__()
        self.reduction = reduction

    def forward(self, input, label, mask=None, weight=None, name=None):
        """BCE Lloss
        """
        loss = F.binary_cross_entropy(input, label, reduction=self.reduction)
        return loss


class MaskL1Loss(nn.Layer):
    """Mask L1 Loss
    """
    def __init__(self, eps=1e-6):
        super(MaskL1Loss, self).__init__()
        self.eps = eps

    def forward(self, pred, gt, mask):
        """Mask L1 Loss
        """
        loss = (paddle.abs(pred - gt) * mask).sum() / (mask.sum() + self.eps)
        loss = paddle.mean(loss)
        return loss


class DBLoss(nn.Layer):
    """Differentiable Binarization (DB) Loss Function
    args:
        param (dict): the super paramter for DB Loss
    """

    def __init__(self,
                 balance_loss=True,
                 main_loss_type='DiceLoss',
                 alpha=5,
                 beta=10,
                 ohem_ratio=3,
                 eps=1e-6,
                 **kwargs):
        super(DBLoss, self).__init__()
        self.alpha = alpha
        self.beta = beta
        self.dice_loss = DiceLoss(eps=eps)
        self.l1_loss = MaskL1Loss(eps=eps)
        self.bce_loss = BalanceLoss(
            balance_loss=balance_loss,
            main_loss_type=main_loss_type,
            negative_ratio=ohem_ratio)
        
    def forward(self, predicts, labels):
        """db loss
        """
        predict_maps = predicts['maps']
        label_threshold_map = labels["threshold_map"]
        label_threshold_mask = labels["threshold_mask"]
        label_shrink_map = labels["shrink_map"]
        label_shrink_mask = labels["shrink_mask"]

        shrink_maps = predict_maps[:, 0, :, :]
        threshold_maps = predict_maps[:, 1, :, :]
        binary_maps = predict_maps[:, 2, :, :]
    
        loss_shrink_maps = self.bce_loss(shrink_maps, label_shrink_map,
                                         label_shrink_mask)
        loss_threshold_maps = self.l1_loss(threshold_maps, label_threshold_map,
                                           label_threshold_mask)
        loss_binary_maps = self.dice_loss(binary_maps, label_shrink_map,
                                          label_shrink_mask)
        loss_shrink_maps = self.alpha * loss_shrink_maps
        loss_threshold_maps = self.beta * loss_threshold_maps

        loss_all = loss_shrink_maps + loss_threshold_maps \
                   + loss_binary_maps
        losses = {'loss': loss_all, \
                  "loss_shrink_maps": loss_shrink_maps, \
                  "loss_threshold_maps": loss_threshold_maps, \
                  "loss_binary_maps": loss_binary_maps}
        return losses

class DBLossLine(nn.Layer):
    """Differentiable Binarization (DB) Loss Function
    args:
        param (dict): the super paramter for DB Loss
    """
    def __init__(self,
                 balance_loss=True,
                 main_loss_type='DiceLoss',
                 alpha=5,
                 beta=10,
                 ohem_ratio=3,
                 eps=1e-6,
                 **kwargs):
        super(DBLossLine, self).__init__()
        self.alpha = alpha
        self.beta = beta
        self.dice_loss = DiceLoss(eps=eps)
        self.l1_loss = MaskL1Loss(eps=eps)
        self.bce_loss = BalanceLoss(
            balance_loss=balance_loss,
            main_loss_type=main_loss_type,
            negative_ratio=ohem_ratio)
        
    def forward(self, predicts, labels):
        """db loss
        """
        predict_maps = predicts['maps']
        label_threshold_map = labels["threshold_map_line"]
        label_threshold_mask = labels["threshold_mask_line"]
        label_shrink_map = labels["shrink_map_line"]
        label_shrink_mask = labels["shrink_mask_line"]

        shrink_maps = predict_maps[:, 0, :, :]
        threshold_maps = predict_maps[:, 1, :, :]
        binary_maps = predict_maps[:, 2, :, :]
    
        loss_shrink_maps = self.bce_loss(shrink_maps, label_shrink_map,
                                         label_shrink_mask)
        loss_threshold_maps = self.l1_loss(threshold_maps, label_threshold_map,
                                           label_threshold_mask)
        loss_binary_maps = self.dice_loss(binary_maps, label_shrink_map,
                                          label_shrink_mask)
        loss_shrink_maps = self.alpha * loss_shrink_maps
        loss_threshold_maps = self.beta * loss_threshold_maps

        loss_all = loss_shrink_maps + loss_threshold_maps \
                   + loss_binary_maps
        losses = {'loss': loss_all, \
                  "loss_shrink_maps": loss_shrink_maps, \
                  "loss_threshold_maps": loss_threshold_maps, \
                  "loss_binary_maps": loss_binary_maps}
        return losses


def get_bias_attr(k):
    """get_bias_attr
    """
    stdv = 1.0 / math.sqrt(k * 1.0)
    initializer = paddle.nn.initializer.Uniform(-stdv, stdv)
    bias_attr = ParamAttr(initializer=initializer)
    return bias_attr


class DBHead(nn.Layer):
    """DB Head
    """
    def __init__(self, in_channels, name_list, kernel_size=3, padding=1):
        super(DBHead, self).__init__()
        self.conv1 = nn.Conv2D(
            in_channels=in_channels,
            out_channels=in_channels // 4,
            kernel_size=kernel_size,
            padding=padding,
            weight_attr=ParamAttr(),
            bias_attr=False)
        self.conv_bn1 = nn.BatchNorm(
            num_channels=in_channels // 4,
            param_attr=ParamAttr(
                initializer=paddle.nn.initializer.Constant(value=1.0)),
            bias_attr=ParamAttr(
                initializer=paddle.nn.initializer.Constant(value=1e-4)),
            act='relu')
        self.conv2 = nn.Conv2DTranspose(
            in_channels=in_channels // 4,
            out_channels=in_channels // 4,
            kernel_size=2,
            stride=2,
            weight_attr=ParamAttr(
                initializer=paddle.nn.initializer.KaimingUniform()),
            bias_attr=get_bias_attr(in_channels // 4))
        self.conv_bn2 = nn.BatchNorm(
            num_channels=in_channels // 4,
            param_attr=ParamAttr(
                initializer=paddle.nn.initializer.Constant(value=1.0)),
            bias_attr=ParamAttr(
                initializer=paddle.nn.initializer.Constant(value=1e-4)),
            act="relu")
        self.conv3 = nn.Conv2DTranspose(
            in_channels=in_channels // 4,
            out_channels=1,
            kernel_size=2,
            stride=2,
            weight_attr=ParamAttr(
                initializer=paddle.nn.initializer.KaimingUniform()),
            bias_attr=get_bias_attr(in_channels // 4), )

    def forward(self, x):
        """forward
        """
        x = self.conv1(x)
        x = self.conv_bn1(x)
        x = self.conv2(x)
        x = self.conv_bn2(x)
        x = self.conv3(x)
        x = F.sigmoid(x)
        return x


class Model(Encoder):
    """ task for e2e text spotting """
    def __init__(self, config, name=''):
        super(Model, self).__init__(config, name=name)
        self.det_config = copy.deepcopy(config['det_module'])
        self.recg_config = copy.deepcopy(config['recg_module'])
        self.labeling_config = copy.deepcopy(config['labeling_module'])

        self.task = config.get('task', 'e2e')
        self.postprocess_cfg = copy.deepcopy(config['postprocess'])

        in_channels = 128
        self.k = 50
        binarize_name_list = [
            'conv2d_56', 'batch_norm_47', 'conv2d_transpose_0', 'batch_norm_48',
            'conv2d_transpose_1', 'binarize'
        ]
        thresh_name_list = [
            'conv2d_57', 'batch_norm_49', 'conv2d_transpose_2', 'batch_norm_50',
            'conv2d_transpose_3', 'thresh'
        ]
        self.binarize = DBHead(in_channels, binarize_name_list)
        self.thresh = DBHead(in_channels, thresh_name_list)
        self.binarize_line = DBHead(in_channels, binarize_name_list)
        self.thresh_line = DBHead(in_channels, thresh_name_list)

        self.db_loss = DBLoss()
        self.det_loss_weight = self.det_config.get('loss_weight')
        self.db_loss_line = DBLossLine()
        self.det_loss_weight_line = self.det_config.get('loss_weight')
        
        self.neck_conv = ConvBNLayer(
            128, 256, [5, 1],
            padding=0,
            name="neck_conv")
        
        # recg_head
        self.method = self.recg_config.get("method")
        self.recg_class_num = self.recg_config.get('num_classes')
        self.recg_seq_len = self.recg_config.get('max_seq_len')
        self.decoder_layers = self.recg_config.get('decoder_layers')
        self.return_intermediate_dec = self.recg_config.get('return_intermediate_dec')
        self.recg_loss = self.recg_config['recg_loss']
        self.recg_loss_weight = self.recg_config['loss_weight']

        self.ocr_recg = RecgHead(
            method=self.method,
            hidden_channels=256, 
            seq_len=self.recg_seq_len, 
            recg_class_num=self.recg_class_num + 2,
            decoder_layers=self.decoder_layers,
            return_intermediate_dec=self.return_intermediate_dec)

        self.label_converter = LabelConverter(
            seq_len=self.recg_seq_len,
            recg_loss=self.recg_loss)
        
        # postprocess config
        self.post_process_thresh = self.postprocess_cfg['thresh']
        self.box_thresh = self.postprocess_cfg['box_thresh']
        self.max_candithresh = self.postprocess_cfg['max_candidates']
        self.unclip_ratio = self.postprocess_cfg['unclip_ratio']
        self.score_mode = self.postprocess_cfg['score_mode']
        self.postprocess = DBPostProcess(
            thresh=self.post_process_thresh,
            box_thresh=self.box_thresh,
            max_candidates=self.max_candithresh,
            unclip_ratio= self.unclip_ratio,
            score_mode=self.score_mode)

        ################### labeling ############################
        num_labels = self.labeling_config['num_labels']
        self.labeling_loss_weight = self.labeling_config['loss_weight']
        self.proposal_w = self.labeling_config['proposal_w']
        self.proposal_h = self.labeling_config['proposal_h']

        d_v_input = self.proposal_h * self.proposal_w * self.out_channels
        self.input_proj = ConvBNLayer(
            in_channels, in_channels - 2, 1,
            act='relu',
            name='proj_conv')
        self.label_classifier = nn.Linear(
            d_v_input,
            num_labels,
            weight_attr=P.ParamAttr(
            name='labeling_cls.w_0',
            initializer=nn.initializer.KaimingNormal()),
            bias_attr=False)
        self.mlm = nn.Linear(
            d_v_input,
            768,
            weight_attr=P.ParamAttr(
                name='token_trans.w_0',
                initializer=nn.initializer.KaimingNormal()),
            bias_attr=True)
        self.word_emb = nn.Embedding(
            30522,
            768,
            weight_attr=P.ParamAttr(
                name='word_embedding',
                initializer=nn.initializer.KaimingNormal()))


    def step_function(self, x, y):
        """step_func
        """
        return paddle.reciprocal(1 + paddle.exp(-self.k * (x - y)))

    def labeling_loss(self, logit, label, label_smooth=-1):
        """ loss """
        label = label.cast('int64')
        num_classes = logit.shape[-1]
        if label_smooth > 0:
            label = F.one_hot(label, num_classes)
            label = F.label_smooth(label, epsilon=label_smooth)
        weight = P.ones([num_classes], dtype='float32')
        if num_classes == 4:
            weight = P.to_tensor([2, 3, 1, 1], dtype='float32')
        if num_classes == 5:
            weight = P.to_tensor([5, 3, 3, 3, 1], dtype='float32')
        loss = F.cross_entropy(logit, label,
            weight=weight,
            soft_label=label_smooth > 0)
        return loss.mean()

    def loss(self, predicts, predicts_line, labels):
        """ loss """
        # det loss
        det_loss_line = self.db_loss_line(predicts_line, labels)
        det_line_loss = det_loss_line['loss']

        # line_labeling loss
        labeling_logit = predicts_line['labeling_logits']
        labeling_label = predicts_line['labeling_gts']
        line_cls_loss = self.labeling_loss(labeling_logit, labeling_label, label_smooth=0.1)

        # token_labeling loss
        labeling_logit = predicts['labeling_logits']
        labeling_label = predicts['labeling_gts']
        token_cls_loss = self.labeling_loss(labeling_logit, labeling_label, label_smooth=0.1)

        # lm loss
        token_logit = predicts['recg_result']
        token_label = predicts['l_texts']
        token_cls = predicts['labeling_gts']
        token_lm_loss = F.cross_entropy(token_logit, token_label.cast('int64'))

        total_loss = det_line_loss + line_cls_loss + 0.1 * token_lm_loss
        losses = {
            "loss": total_loss, 
            "det_loss": det_loss,
            "cls_loss": cls_loss,
            "lm_loss": 0.1 * token_lm_loss}

        return losses

    def distort_bboxes(self, bboxes, ori_h, ori_w, pad_scale=1):
        """distort bboxes
        Args: 
            bboxes: [num, 4]
            ori_h: the height of the image
            ori_w: the width of the image
        """
        pad = paddle.to_tensor([-1, -1, 1, 1], dtype='float32') * pad_scale
        offset = paddle.to_tensor(np.random.randint(-pad_scale, pad_scale + 1, size=bboxes.shape), dtype='float32')
        pad = pad + offset
        bboxes = bboxes + pad

        bboxes[:, ::2] = bboxes[:, ::2].clip(0, ori_w)
        bboxes[:, 1::2] = bboxes[:, 1::2].clip(0, ori_h)
        return bboxes

    def over_sample(self, bboxes, texts):
        """over sample long-text instance
        Args:
            bboxes: [num, 4]
            texts: [num, seq_len]
        """
        # TODO add fix num sample
        sampled_bboxes = []
        sampled_texts = []

        for i in range(bboxes.shape[0]):
            len_text = paddle.nonzero(texts[i]).shape[0] - 1 # -1 for the [Stop] symbol

            if len_text > 36:
                num_sample = 10
            elif len_text > 12:
                num_sample = 5
            else:
                num_sample = 1
  
            sampled_bbox = paddle.tile(bboxes[i, :], repeat_times=[num_sample, 1])
            sampled_text = paddle.tile(texts[i, :], repeat_times=[num_sample, 1])
            sampled_bboxes.append(sampled_bbox)
            sampled_texts.append(sampled_text)

        bboxes = paddle.concat(sampled_bboxes, axis=0)
        texts = paddle.concat(sampled_texts, axis=0)

        return bboxes, texts

    def pad_rois_w(self, rois):
        """padding bbox width to the same width
        Args:
            rois: [num, 4]
        Returns:
            rois_padded: [num, 4]
            rois_masks: [num, 1, 1, w_max] 
        """
        rois = rois.cast('int32')
        num = rois.shape[0]
        rois_w = paddle.abs(rois[:, 2] - rois[:, 0])  # [num]
        rois_w_max = paddle.max(rois_w, axis=-1)
        rois[:, 2] = paddle.clip(rois[:, 0] + rois_w_max, min=0, max=959)

        rois_masks = paddle.zeros([num, rois_w_max], dtype='int32')
        for i in range(num):
            if rois_w[i] == 0: # boundary condition
                rois_masks[i, :] = 1
            else:
                rois_masks[i, :rois_w[i]] = 1

        return rois.cast('float32'), rois_masks.unsqueeze(-2).unsqueeze(-2), rois_w_max

    def forward(self, *args, **kwargs):
        """ forword """
        feed_names = kwargs.get('feed_names')
        input_data = dict(zip(feed_names, args))
        is_train = kwargs.get('is_train', False)
        eval_with_gt_bbox = kwargs.get('eval_with_gt_bbox', False)

        image = input_data['image']
        bs, _, ori_h, ori_w = image.shape

        # backbone
        enc_out = super(Model, self).forward([image, None])
        enc_out = enc_out['additional_info']['image_feat']
        x = enc_out['out']  # [bs, 128, h, w]

        # labeling
        enc_final = self.input_proj(x)
        x_range = P.linspace(-1, 1, P.shape(enc_final)[-1], dtype='float32')
        y_range = P.linspace(-1, 1, P.shape(enc_final)[-2], dtype='float32')
        yy, xx = P.meshgrid([y_range, x_range])
        xx = P.unsqueeze(xx, [0, 1])
        yy = P.unsqueeze(yy, [0, 1])
        yy = P.expand(yy, shape=[P.shape(enc_final)[0], 1, -1, -1])
        xx = P.expand(xx, shape=[P.shape(enc_final)[0], 1, -1, -1])
        coord_feat = P.concat([xx, yy], axis=1)
        enc_final = P.concat([enc_final, coord_feat], axis=1)

        results, results_line = {}, {}
        # detection
        '''
        ## word
        shrink_maps = self.binarize(x)  # [1, 1, 960, 960]
        threshold_maps = self.thresh(x)  # [1, 1, 960, 960]
        binary_maps = self.step_function(shrink_maps, threshold_maps)
        y = paddle.concat([shrink_maps, threshold_maps, binary_maps], axis=1)
        results['maps'] = y
        '''
        ## line
        shrink_maps_line = self.binarize_line(x)  # [1, 1, 960, 960]
        threshold_maps_line = self.thresh_line(x)  # [1, 1, 960, 960]
        binary_maps_line = self.step_function(shrink_maps_line, threshold_maps_line)
        y_line = paddle.concat([shrink_maps_line, threshold_maps_line, binary_maps_line], axis=1)
        results_line['maps'] = y

        # recognition
        rois_num = []
        rois_num_ = []
        rois = []
        rois_ = []

        if is_train:
            bboxes_padded_list = input_data['bboxes_padded_list']  # [bs, 512, 4]
            texts_padded_list = input_data['texts_padded_list']
            masks_padded_list = input_data['masks_padded_list']
            classes_padded_list = input_data['classes_padded_list']
            texts_label = []
            classes_label = []
        
            for b in range(bs):
                bboxes = bboxes_padded_list[b]
                texts = texts_padded_list[b] 
                masks = masks_padded_list[b] 
                classes = classes_padded_list[b]
                bool_idxes = paddle.nonzero(masks) 
                
                if(bool_idxes.shape[0] == 0):
                    rois_num.append(0)
                    rois.append(paddle.to_tensor([], stop_gradient=False))
                    continue
        
                bboxes = paddle.index_select(bboxes, bool_idxes)  # [num, 4]
                texts = paddle.index_select(texts, bool_idxes)  # [num, 50]
                classes = paddle.index_select(classes, bool_idxes) # [num, 1]

                # distort the bbox
                #bboxes = self.distort_bboxes(bboxes, ori_h, ori_w, pad_scale=1)
                
                rois_num.append(bboxes.shape[0])
                rois.append(bboxes)
                texts_label.append(texts)
                classes_label.append(classes)
   
            rois_num = paddle.to_tensor(rois_num, dtype='int32')
            texts_label = paddle.concat(texts_label, axis=0)
            rois = paddle.concat(rois, axis=0) # [bs*num, 4]
     
            roi_feat = roi_align(
                enc_final,
                rois, 
                boxes_num=rois_num,
                output_size=(self.proposal_h, self.proposal_w),
                spatial_scale=0.25)
            roi_feat = roi_feat.reshape(roi_feat.shape[:1] + [-1]) # [bs*num, 128*4*64]

            token_feat = self.mlm(roi_feat)
            token_logit = token_feat.matmul(self.word_emb.weight, transpose_y=True)
            labeling_logit = self.label_classifier(roi_feat) # [bs*num, 5]
      
            results['recg_result'] = token_logit
            results['l_texts'] = texts_label[:, 0]
            results['labeling_logits'] = labeling_logit
            results['labeling_gts'] = classes_label

            ########################## labeling ########################################
            bboxes_padded_list_line = input_data['bboxes_padded_list_line']  # [bs, 512, 4]
            masks_padded_list_line = input_data['masks_padded_list_line']
            classes_padded_list_line = input_data['classes_padded_list_line']
            classes_label_line = []

            for b in range(bs):
                bboxes = bboxes_padded_list_line[b]
                masks = masks_padded_list_line[b]
                classes = classes_padded_list_line[b]
                bool_idxes = paddle.nonzero(masks)

                if(bool_idxes.shape[0] == 0):
                    rois_num_.append(0)
                    rois_.append(paddle.to_tensor([], stop_gradient=False))
                    continue

                bboxes = paddle.index_select(bboxes, bool_idxes)  # [num, 4]
                classes = paddle.index_select(classes, bool_idxes) # [num, 1]

                # oversample for long text
                # bboxes, texts = self.over_sample(bboxes, texts)

                # distort the bbox
                bboxes = self.distort_bboxes(bboxes, ori_h, ori_w, pad_scale=1)

                rois_num_.append(bboxes.shape[0])
                rois_.append(bboxes)
                classes_label_line.append(classes)

            rois_num_ = paddle.to_tensor(rois_num_, dtype='int32')
            classes_label_line = paddle.concat(classes_label_line, axis=0) # [bs*num, ]
            rois_ = paddle.concat(rois_, axis=0) # [bs*num, 4]
            roi_feat = roi_align(
                        enc_final,
                        rois_,
                        boxes_num=rois_num_,
                        output_size=(self.proposal_h, self.proposal_w),
                        spatial_scale=0.25) # [bs*num, 128, 4, 64]
            roi_feat = roi_feat.reshape(roi_feat.shape[:1] + [-1]) # [bs*num, 128*4*64]

            labeling_logit = self.label_classifier(roi_feat) # [bs*num, 5]
            results_line['labeling_logits'] = labeling_logit # [bs*num, 5]
            results_line['labeling_gts'] = classes_label_line # [bs*num,]
            ######################################### labeling end ###################################
            losses = self.loss(results, results_line, input_data)
            results.update(losses)
            return results

        # eval branch and infer branch
        # if eval_with_gt_bbox, we only use gt_bbox as our rois, else we use the detection result as our roi
        if eval_with_gt_bbox:
            bboxes_padded_list = input_data['bboxes_padded_list']  # [bs, 512, 4]
            texts_padded_list = input_data['texts_padded_list']
            masks_padded_list = input_data['masks_padded_list']
            bboxes_4pts_padded_list = input_data['bboxes_4pts_padded_list']

            bs, c, h, w = x.shape
            rois_num = []
            rois = []
            texts_label = []
            bboxes_4pts_label = []   

            for b in range(bs):
                bboxes = bboxes_padded_list[b]
                bboxes_4pts = bboxes_4pts_padded_list[b]
                texts = texts_padded_list[b]  
                masks = masks_padded_list[b] 
                bool_idxes = paddle.nonzero(masks) 
                
                if(bool_idxes.shape[0] == 0):
                    rois_num.append(0)
                    rois.append(paddle.to_tensor([], stop_gradient=False))
                    continue
                
                bboxes = paddle.index_select(bboxes, bool_idxes)  # [num, 4]
                bboxes_4pts = paddle.index_select(bboxes_4pts, bool_idxes)
                texts = paddle.index_select(texts, bool_idxes)  # [num, 50]
                
                rois_num.append(bboxes.shape[0])
                rois.append(bboxes)
                bboxes_4pts_label.append(bboxes_4pts)
                texts_label.append(texts)


            rois_num = paddle.to_tensor(rois_num, dtype='int32')
            rois = paddle.concat(rois, axis=0)  # [bs*num, 4]
            # rois, rois_masks, roi_w = self.pad_rois_w(rois)
            roi_w = 50
            roi_feat = roi_align(
                x,
                rois, 
                boxes_num=rois_num,
                output_size=(5, roi_w),
                spatial_scale=0.25)

            # roi_feat *= rois_masks
            neck_feat = self.neck_conv(roi_feat)  # [bs*num, 256, 1,50]
            recg_out = self.ocr_recg(neck_feat)[-1]
  
            num_idx = 0
            recg_result = []
            recg_label = []

            for num in rois_num:
                recg_result.append(recg_out[num_idx: (num_idx + num)])
                num_idx += num  

            bbox_out = []
            for idx in range(bs):
                bbox_out_single = {'points': bboxes_4pts_label[idx]}
                bbox_out.append(bbox_out_single)

            results = {'det_result': bbox_out, 'recg_result': recg_result}
            results['e2e_preds'] = self.inference(results)
        
            gt_labels = {'det_label': bboxes_4pts_label, 'recg_label': texts_label}
            results['e2e_gts'] = self.prepare_labels(gt_labels)
            
            return results
        else:
            results = {}
            #results = {'maps': shrink_maps}
            results_line = {'maps': shrink_maps_line}
            # when evaling and infereing, we send the detection area to recognition head

            shape_list = [(image[i].shape[1], image[i].shape[2], 1, 1) for i in range(image.shape[0])]
            #bbox_out = self.postprocess(results, shape_list)
            bbox_out_line = self.postprocess(results_line, shape_list)

            '''
            for b in range(bs):
                pred_res = bbox_out[b]['points']  # [num, 4, 2] nd_array
                pt1 = pred_res[:, 0, :]
                pt2 = pred_res[:, 2, :]
                bboxes = np.concatenate((pt1, pt2), axis=-1)
                bboxes = paddle.to_tensor(bboxes, dtype='float32')  # [num, 4]
                rois_num.append(bboxes.shape[0])
                rois.append(bboxes)

            rois_num = paddle.to_tensor(rois_num, dtype='int32')
            rois = paddle.concat(rois, axis=0)
            # rois, rois_masks, roi_w = self.pad_rois_w(rois)
            roi_w = 50
            roi_feat = roi_align(
                x,
                rois,
                output_size=(5, roi_w),
                spatial_scale=0.25,
                boxes_num=rois_num)

            # roi_feat *= rois_masks
            neck_feat = self.neck_conv(roi_feat)
            recg_out = self.ocr_recg(neck_feat)[-1]

            recg_result = []
            recg_label = []

            num_idx = 0
            for num in rois_num:
                recg_result.append(recg_out[num_idx: (num_idx + num)])
                num_idx += num

            pred_labels = {'det_result': bbox_out, 'recg_result': recg_result}
            results['e2e_preds'] = self.inference(pred_labels)
            '''

            ##################### line #######################################
            results['line_preds'] = []
            for b in range(bs):
                pred_res = bbox_out_line[b]['points']  # [num, 4, 2] nd_array
                if(pred_res.shape[0] == 0):
                    results['line_preds'].append([])
                else:
                    pt1 = pred_res[:, 0, :]
                    pt2 = pred_res[:, 2, :]
                    bboxes = np.concatenate((pt1, pt2), axis=-1)
                    bboxes = paddle.to_tensor(bboxes, dtype='float32')  # [num, 4]
                    rois_num_.append(bboxes.shape[0])
                    rois_.append(bboxes)

                    rois_num_ = paddle.to_tensor(rois_num_, dtype='int32')
                    rois_ = paddle.concat(rois_, axis=0)

                    roi_feat = roi_align(
                                enc_final,
                                rois_,
                                boxes_num=rois_num_,
                                output_size=(self.proposal_h, self.proposal_w),
                                spatial_scale=0.25) # [bs*num, 128, 4, 64]
                    roi_feat = roi_feat.reshape(roi_feat.shape[:1] + [-1]) # [bs*num, 128*4*64]
                    labeling_logit = self.label_classifier(roi_feat) # [bs*num, 5]
                    results['labeling_preds'] = P.argmax(labeling_logit, axis=-1) # [bs*num, 5]

                    pred_labels_line = {'det_result': [bbox_out_line[b]], 'class_result': [results['labeling_preds']]}
                    results['line_preds'] += self.inference(pred_labels_line)
            ##################### line #####################################

            # for det only
            '''
            pred_labels = {'det_result': bbox_out}
            results['det4classes_preds'] = self.inference(pred_labels)
            '''

            # prepare eval labels for eval
            if input_data.__contains__('texts_padded_list'):
                ''' 
                bboxes_padded_list = input_data['bboxes_4pts_padded_list']
                texts_padded_list = input_data['texts_padded_list']
                masks_padded_list = input_data['masks_padded_list']
                classes_padded_list = input_data['classes_padded_list']
                texts_label = []
                bboxes_label = []
                classes_label = []
                for b in range(bs):
                    bboxes = bboxes_padded_list[b]  # [512, 4]
                    texts = texts_padded_list[b]  # [512, 50]
                    text_classes = classes_padded_list[b]
                    masks = masks_padded_list[b]
                    bool_idxes = paddle.nonzero(masks) # [38,1]

                    bboxes = paddle.index_select(bboxes, bool_idxes)
                    texts = paddle.index_select(texts, bool_idxes)
                    classes = paddle.index_select(text_classes, bool_idxes)
                    bboxes_label.append(bboxes)
                    texts_label.append(texts)
                    classes_label.append(text_classes)

                gt_labels = {'det_label': bboxes_label, 'recg_label': texts_label}
                results['e2e_gts'] = self.prepare_labels(gt_labels)
                gt_labels = {'det_label':bboxes_label, 'class_label':classes_label}
                results['det4classes_gts'] = self.prepare_labels(gt_labels)
                ''' 

                ## line
                bboxes_padded_list = input_data['bboxes_4pts_padded_list_line']
                texts_padded_list = input_data['texts_padded_list_line']
                masks_padded_list = input_data['masks_padded_list_line']
                classes_padded_list = input_data['classes_padded_list_line']
                texts_label_line = []
                bboxes_label_line = []
                classes_label_line = []
                for b in range(bs):
                    bboxes = bboxes_padded_list[b]  # [512, 4]
                    texts = texts_padded_list[b]  # [512, 50]
                    text_classes = classes_padded_list[b]
                    masks = masks_padded_list[b]
                    bool_idxes = paddle.nonzero(masks) # [38,1]

                    bboxes = paddle.index_select(bboxes, bool_idxes)
                    texts = paddle.index_select(texts, bool_idxes)
                    classes = paddle.index_select(text_classes, bool_idxes)
                    bboxes_label_line.append(bboxes)
                    texts_label_line.append(texts)
                    classes_label_line.append(text_classes)

                gt_labels_line = {'det_label': bboxes_label_line, 'class_label': classes_label_line}
                results['line_gts'] = self.prepare_labels(gt_labels_line)

            return results

    def inference(self, raw_results):
        """
        Output: poly, text, score
        """
        batch_size = len(raw_results['det_result'])
        processed_results = []

        for bs_idx in range(batch_size):
            processed_result = []
            res_num = len(raw_results['det_result'][bs_idx]['points'])
            for idx in range(res_num):
                poly = raw_results['det_result'][bs_idx]['points'][idx]
                if isinstance(poly, paddle.Tensor):
                    poly = poly.tolist()
                else:
                    poly = poly.reshape(-1).tolist()

                if raw_results.__contains__('recg_result'):
                    transcript = raw_results['recg_result'][bs_idx][idx]
                    word, prob = self.decode_transcript(transcript)
                    processed_result.append([poly, word, prob])
                elif raw_results.__contains__('class_result'):
                    text_class = raw_results['class_result'][bs_idx][idx]
                    text_class = text_class.numpy().astype(np.int32).item()
                    prob = 1.0
                    processed_result.append([poly, str(text_class), prob])
                else:
                    processed_result.append(poly)
            processed_results.append(processed_result)
        return processed_results

    def decode_transcript(self, pred_recg):
        """decode_transcript
        """
        _, preds_index = pred_recg.topk(1, axis=-1, largest=True, sorted=True)
        probs = paddle.nn.functional.softmax(pred_recg, axis=-1)
        probs = probs.topk(1, axis=-1, largest=True, sorted=True)[0].reshape([-1])
        preds_index = preds_index.reshape([-1])
        word = self.label_converter.decode(preds_index)
        prob = 0.0 if len(word) == 0 else probs[:len(word)].mean().numpy()[0]

        return word, prob

    def prepare_labels(self, raw_results):
        """preapre labels for validation
        """
        batch_size = len(raw_results['det_label'])
        processed_results = []

        for bs_idx in range(batch_size):
            processed_result = []
            res_num = len(raw_results['det_label'][bs_idx])
            for idx in range(res_num):
                poly = raw_results['det_label'][bs_idx][idx]
                poly = poly.numpy().astype(np.int32).tolist()

                # for det only
                if raw_results.__contains__('class_label'):
                    text_class = raw_results['class_label'][bs_idx][idx]
                    text_class = text_class.numpy().astype(np.int32).item()
                    ignore = False
                    processed_result.append([poly, str(text_class), ignore])
                else:
                    transcript = raw_results['recg_label'][bs_idx][idx]
                    word = self.label_converter.decode(transcript)
                    # ignore = True if len(word) == 0 else False
                    ignore = False
                    processed_result.append([poly, word, ignore])
            processed_results.append(processed_result)
        return processed_results
