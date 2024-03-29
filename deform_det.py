import os, time, sys, math, random, glob, datetime
sys.path.append(os.path.expanduser("~/Documents"))
import cv2, torch
import numpy as np

import omni_torch.utils as util
from omni_torch.networks.optimizer.adabound import AdaBound
import omni_torch.visualize.basic as vb

import dd_data as data
import dd_preset as preset
import dd_model as model
from dd_loss import MultiBoxLoss
from dd_utils import *
from dd_preprocess import *
from dd_augment import *
from dd_postprocess import combine_boxes
from dd_vis import visualize_bbox, print_box

PIC = os.path.expanduser("~/Pictures/")
TMPJPG = os.path.expanduser("~/Pictures/tmp.jpg")
opt = preset.parse_arguments()
args = util.get_args(preset.PRESET, opt=opt)
cfg = model.cfg
cfg['super_wide'] = args.cfg_super_wide
cfg['super_wide_coeff'] = args.cfg_super_wide_coeff
cfg['overlap_thresh'] = args.jaccard_distance_threshold


def fit(args, cfg, net, detector, dataset, optimizer, is_train):
    def avg(list):
        return sum(list) / len(list)
    if is_train:
        net.train()
    else:
        net.eval()
    Loss_L, Loss_C = [], []
    epoch_eval_results = {}
    for epoch in range(args.epoches_per_phase):
        visualize = False
        if args.curr_epoch % 5 == 0 and epoch == 0:
            print("Visualizing prediction result at %d th epoch %d th iteration"%(args.curr_epoch, epoch))
            visualize = True
        start_time = time.time()
        criterion = MultiBoxLoss(cfg, neg_pos=3, use_focal=args.focal_loss, focal_power=args.focal_power)
        # Update variance and balance of loc_loss and conf_loss
        cfg['variance'] = [var * cfg['var_updater'] if var <= 0.95 else 1 for var in cfg['variance']]
        cfg['alpha'] *= cfg['alpha_updater']
        for batch_idx, (images, targets) in enumerate(dataset):
            #if not net.fix_size:
                #assert images.size(0) == 1, "batch size for dynamic input shape can only be 1 for 1 GPU RIGHT NOW!"
            if len(targets) == 0:
                continue
            images = images.cuda()
            ratios = images.size(3) / images.size(2)
            if ratios != 1.0:
                print(ratios)
            targets = [ann.cuda() for ann in targets]
            out = net(images, is_train)
            if args.curr_epoch == 0 and batch_idx == 0:
                #visualize_bbox(args, cfg, images, targets, net.module.prior, batch_idx)
                pass
            if is_train:
                try:
                    loss_l, loss_c = criterion(out, targets, ratios)
                except RuntimeError:
                    print("error in calculate loss_c")
                    continue
                loss = loss_l + loss_c
                Loss_L.append(float(loss_l.data))
                Loss_C.append(float(loss_c.data))
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
            else:
                # Turn the input param detector into None so as to
                # Experiment with Detector's Hyper-parameters
                create_detector = True if detector is None else False
                for _i, top_k in enumerate([500]):
                    for _j, conf_thres in enumerate([0.05]):
                        for _k, nms_thres in enumerate([0.3]):
                            eval_thres = [0.1]
                            key = "%s_%s_%s"%(top_k, conf_thres, nms_thres)
                            if key in epoch_eval_results:
                                batch_result = epoch_eval_results[key]
                            else:
                                batch_result = {}
                            if create_detector:
                                detector = model.Detect(num_classes=2, bkg_label=0, top_k=top_k,
                                                        conf_thresh=conf_thres, nms_thresh=nms_thres)
                            loc_data, conf_data, prior_data = out
                            det_result = detector(loc_data, conf_data, prior_data)
                            eval_result = evaluate(images, det_result.data, targets, batch_idx, eval_thres,
                                                   visualize=visualize, post_combine=True)
                            for _key in eval_result.keys():
                                if _key in batch_result:
                                    batch_result[_key] += eval_result[_key]
                                else:
                                    batch_result.update({_key: eval_result[_key]})
                            epoch_eval_results.update({key: batch_result})
        if is_train:
            args.curr_epoch += 1
            print(" --- loc loss: %.4f, conf loss: %.4f, at epoch %04d, cost %.2f seconds ---" %
                  (avg(Loss_L), avg(Loss_C), args.curr_epoch + 1, time.time() - start_time))
    if not is_train:
        for key in sorted(epoch_eval_results.keys()):
            keys = key.split("_")
            print("top_k: %s, conf_thres: %s, nms_thres: %s"%(keys[0], keys[1], keys[2]))
            for _key in sorted(epoch_eval_results[key]):
                eval = np.mean(np.asarray(epoch_eval_results[key][_key]).reshape((-1, 4)), axis=0)
                print(" --- Conf=%s: accuracy=%.4f, precision=%.4f, recall=%.4f, f1-score=%.4f  ---" %
                  (_key, eval[0], eval[1], eval[2], eval[3]))
            print("")
        # represent accuracy, precision, recall, f1_score
        return  eval[0], eval[1], eval[2], eval[3]
    else:
        return avg(Loss_L), avg(Loss_C)


def val(args, cfg, net, dataset, optimizer, prior):
    with torch.no_grad():
        fit(args, cfg, net, dataset, optimizer, prior, False)


def evaluate(img, detections, targets, batch_idx, eval_thres, visualize=False, post_combine=False):
    eval_result = {}
    save_dir = os.path.expanduser("~/Pictures/")
    w = img.size(3)
    h = img.size(2)
    for threshold in eval_thres:
        idx = detections[0, 1, :, 0] >= threshold
        _boxes = detections[0, 1, idx, 1:]
        gt_boxes = targets[0][:, :-1].data
        if gt_boxes.size(0) == 0:
            print("No ground truth box in this patch")
            break
        if _boxes.size(0) == 0:
            print("No predicted box in this patch")
            break
        boxes = combine_boxes(_boxes, img=img)
        jac = jaccard(boxes, gt_boxes)
        overlap, idx = jac.max(1, keepdim=True)
        # This is not DetEval
        positive_pred = boxes[overlap.squeeze(1) > 0.2]
        negative_pred = boxes[overlap.squeeze(1) <= 0.2]
        if negative_pred.size(0) == 0:
            negative_pred = tuple()
        #print_box(blue_boxes=positive_pred, green_boxes=gt_boxes, red_boxes=negative_pred,
                  #img=vb.plot_tensor(args, img, margin=0), save_dir=save_dir)

        accuracy, precision, recall = measure(positive_pred, gt_boxes, width=w, height=h)
        if (recall + precision) < 1e-3:
            f1_score = 0
        else:
            f1_score = 2 * (recall * precision) / (recall + precision)
        if visualize and threshold == 0.1:
            pred = [[float(coor) for coor in area] for area in positive_pred]
            gt = [[float(coor) for coor in area] for area in gt_boxes]
            print_box(negative_pred, green_boxes=gt, blue_boxes=pred, idx=batch_idx,
                      img=vb.plot_tensor(args, img, margin=0), save_dir=args.val_log)
        eval_result.update({threshold: [accuracy, precision, recall, f1_score]})
    return eval_result


def test():
    aug = aug_test(args)
    dataset = data.fetch_detection_data(args, sources=args.test_sources,
                                        k_fold=1, auxiliary_info=args.test_aux, aug=aug,
                                        batch_size=1 / torch.cuda.device_count(), shuffle=False)[0][0]
    net = model.SSD(cfg, connect_loc_to_conf=args.loc_to_conf, fix_size=args.fix_size,
                    conf_incep=args.conf_incep, loc_incep=args.loc_incep, nms_thres=args.nms_threshold,
                    loc_preconv=args.loc_preconv, conf_preconv=args.conf_preconv,
                    FPN=args.feature_pyramid_net, SA=args.self_attention,
                    in_wid=args.inner_filters, m_factor=args.inner_m_factor)
    net = torch.nn.DataParallel(net, device_ids=[0], output_device=0).cuda()
    net = util.load_latest_model(args, net, prefix=args.model_prefix_finetune, strict=True)
    detector = model.Detect(num_classes=2, bkg_label=0, top_k=args.detector_top_k,
                            conf_thresh=args.detector_conf_threshold, nms_thresh=args.detector_nms_threshold)
    with torch.no_grad():
        for batch_idx, (images, targets) in enumerate(dataset):
            images = images.cuda()
            print(images.shape)
            targets = [ann.cuda() for ann in targets]
            ratios = images.size(3) / images.size(2)
            if ratios != 1.0:
                print(ratios)
            out = net(images,  is_train=False)
            loc_data, conf_data, prior_data = out
            #prior_data = prior_data.to("cuda:%d" % (device_id))
            det_result = detector(loc_data, conf_data, prior_data)
            eval_result = evaluate(images, det_result.data, targets, batch_idx, args.threshold,
                                   visualize=True, post_combine=True)
            print(eval_result)


def main():
    aug = aug_temp(args)
    dt = datetime.datetime.now().strftime("%Y-%m-%d_%H:%M")
    datasets = data.fetch_detection_data(args, sources=args.train_sources, k_fold=1,
                                         batch_size=args.batch_size_per_gpu, batch_size_val=1,
                                         auxiliary_info=args.train_aux, split_val=0.1, aug=aug)
    for idx, (train_set, val_set) in enumerate(datasets):
        loc_loss, conf_loss = [], []
        accuracy, precision, recall, f1_score = [], [], [], []
        print("\n =============== Cross Validation: %s/%s ================ " %
              (idx + 1, len(datasets)))
        net = model.SSD(cfg, connect_loc_to_conf=args.loc_to_conf, fix_size=args.fix_size,
                        conf_incep=args.conf_incep, loc_incep=args.loc_incep, nms_thres=args.nms_threshold,
                        loc_preconv=args.loc_preconv, conf_preconv=args.conf_preconv,
                        FPN=args.feature_pyramid_net, SA=args.self_attention,
                        in_wid = args.inner_filters, m_factor = args.inner_m_factor)
        net = torch.nn.DataParallel(net, device_ids=args.gpu_id, output_device=args.output_gpu_id).cuda()
        detector = model.Detect(num_classes=2, bkg_label=0, top_k=800, conf_thresh=0.05, nms_thresh=0.3)
       #detector = None
        # Input dimension of bbox is different in each step
        torch.backends.cudnn.benchmark = True
        if args.fix_size:
            net.module.prior = net.module.prior.cuda()
        if args.finetune:
            net = util.load_latest_model(args, net, prefix=args.model_prefix_finetune, strict=True)
        # Using the latest optimizer, better than Adam and SGD
        optimizer = AdaBound(net.parameters(), lr=args.learning_rate, final_lr=20*args.learning_rate,
                             weight_decay=args.weight_decay,)

        for epoch in range(args.epoch_num):
            loc_avg, conf_avg = fit(args, cfg, net, detector, train_set, optimizer, is_train=True)
            loc_loss.append(loc_avg)
            conf_loss.append(conf_avg)
            train_losses = [np.asarray(loc_loss), np.asarray(conf_loss)]
            if val_set is not None:
                accu, pre, rec, f1 = fit(args, cfg, net, detector, val_set, optimizer, is_train=False)
                accuracy.append(accu)
                precision.append(pre)
                recall.append(rec)
                f1_score.append(f1)
                val_losses = [np.asarray(accuracy), np.asarray(precision),
                              np.asarray(recall), np.asarray(f1_score)]
            if epoch != 0 and epoch % 10 == 0:
                util.save_model(args, args.curr_epoch, net.state_dict(), prefix=args.model_prefix,
                                keep_latest=3)
            if epoch > 5:
                vb.plot_multi_loss_distribution(
                    multi_line_data=[train_losses, val_losses],
                    multi_line_labels=[["location", "confidence"], ["Accuracy", "Precision", "Recall", "F1-Score"]],
                    save_path=args.loss_log, window=5, name=dt,
                    bound=[ {"low": 0.0, "high": 3.0}, {"low": 0.0, "high": 1.0}],
                    titles=["Train Loss", "Validation Score"]
                )
        # Clean the data for next cross validation
        del net, optimizer
        args.curr_epoch = 0


if __name__ == "__main__":
    if args.train:
        main()
    if args.test:
        test()


