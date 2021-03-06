import os
import time
import argparse
import numpy as np
import saverloader

from fire import Fire

from nets.segnet import Segnet

import utils.misc
import utils.improc
import utils.vox
import random

from nuscenesdataset import compile_data

import torch
torch.multiprocessing.set_sharing_strategy('file_system')
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader

from tensorboardX import SummaryWriter

import torch.nn.functional as F

random.seed(125)
np.random.seed(125)

scene_centroid_x = 0.0
scene_centroid_y = 1.0
scene_centroid_z = 0.0

scene_centroid = np.array([scene_centroid_x,
                           scene_centroid_y,
                           scene_centroid_z]).reshape([1, 3])
scene_centroid = torch.from_numpy(scene_centroid).float()

XMIN, XMAX = -50, 50
ZMIN, ZMAX = -50, 50
YMIN, YMAX = -5, 5
bounds = (XMIN, XMAX, YMIN, YMAX, ZMIN, ZMAX)

Z, Y, X = 200, 8, 200

def requires_grad(parameters, flag=True):
    for p in parameters:
        p.requires_grad = flag

class SimpleLoss(torch.nn.Module):
    def __init__(self, pos_weight):
        super(SimpleLoss, self).__init__()
        self.loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight=torch.Tensor([pos_weight]), reduction='none')

    def forward(self, ypred, ytgt, valid):
        loss = self.loss_fn(ypred, ytgt)
        loss = utils.basic.reduce_masked_mean(loss, valid)
        return loss

def balanced_mse_loss(pred, gt, valid=None):
    pos_mask = gt.gt(0.5).float()
    neg_mask = gt.lt(0.5).float()
    if valid is None:
        valid = torch.ones_like(pos_mask)
    mse_loss = F.mse_loss(pred, gt, reduction='none')
    pos_loss = utils.basic.reduce_masked_mean(mse_loss, pos_mask*valid)
    neg_loss = utils.basic.reduce_masked_mean(mse_loss, neg_mask*valid)
    loss = (pos_loss + neg_loss)*0.5
    return loss

def balanced_ce_loss(out, target, valid):
    B, N, H, W = out.shape
    total_loss = torch.tensor(0.0).to(out.device)
    normalizer = 0
    NN = valid.shape[1]
    assert(NN==1)
    for n in range(N):
        out_ = out[:,n]
        tar_ = target[:,n]
        val_ = valid[:,0]

        pos = tar_.gt(0.99).float()
        neg = tar_.lt(0.95).float()
        label = pos*2.0 - 1.0
        a = -label * out_
        b = F.relu(a)
        loss = b + torch.log(torch.exp(-b)+torch.exp(a-b))
        if torch.sum(pos*val_) > 0:
            pos_loss = utils.basic.reduce_masked_mean(loss, pos*val_)
            neg_loss = utils.basic.reduce_masked_mean(loss, neg*val_)
            total_loss += (pos_loss + neg_loss)*0.5
            normalizer += 1
        else:
            total_loss += loss.mean()
            normalizer += 1
    return total_loss / normalizer
    
def balanced_occ_loss(pred, occ, free):
    pos = occ.clone()
    neg = free.clone()

    label = pos*2.0 - 1.0
    a = -label * pred
    b = F.relu(a)
    loss = b + torch.log(torch.exp(-b)+torch.exp(a-b))

    mask_ = (pos+neg>0.0).float()

    pos_loss = utils.basic.reduce_masked_mean(loss, pos)
    neg_loss = utils.basic.reduce_masked_mean(loss, neg)

    balanced_loss = pos_loss + neg_loss

    return balanced_loss

def run_model(model, loss_fn, d, eff_B, eff_T, device='cuda:0', sw=None, is_train=True):
    metrics = {}

    imgs, rots, trans, intrins, pts0, extra0, pts, extra, lrtlist_velo, vislist, tidlist, scorelist, seg_bev_g, valid_bev_g, center_bev_g, offset_bev_g, radar_data, egopose = d

    # sometimes the batch size is too large to fit into the gpus
    # we factor the original batch size into B*T, where:
    #     B is the effective batch size in each forward/backward pass
    #     T is the imaginary "timesteps" of forward/backward passes we need to run
    B=eff_B
    T=eff_T
    imgs = imgs.reshape(B, T, *imgs.size()[2:])
    rots = rots.reshape(B, T, *rots.size()[2:])
    trans = trans.reshape(B, T, *trans.size()[2:])
    intrins = intrins.reshape(B, T, *intrins.size()[2:])
    pts0 = pts0.reshape(B, T, *pts0.size()[2:])
    extra0 = extra0.reshape(B, T, *extra0.size()[2:])
    pts = pts.reshape(B, T, *pts.size()[2:])
    extra = extra.reshape(B, T, *extra.size()[2:])
    lrtlist_velo = lrtlist_velo.reshape(B, T, *lrtlist_velo.size()[2:])
    vislist = vislist.reshape(B, T, *vislist.size()[2:])
    tidlist = tidlist.reshape(B, T, *tidlist.size()[2:])
    scorelist = scorelist.reshape(B, T, *scorelist.size()[2:])
    seg_bev_g = seg_bev_g.reshape(B, T, *seg_bev_g.size()[2:])
    valid_bev_g = valid_bev_g.reshape(B, T, *valid_bev_g.size()[2:])
    center_bev_g = center_bev_g.reshape(B, T, *center_bev_g.size()[2:])
    offset_bev_g = offset_bev_g.reshape(B, T, *offset_bev_g.size()[2:])
    radar_data = radar_data.reshape(B, T, *radar_data.size()[2:])
    egopose = egopose.reshape(B, T, *egopose.size()[2:])

    B0,T,S,C,H,W = imgs.shape
    __p0 = lambda x: utils.basic.pack_seqdim(x, B0)
    __u0 = lambda x: utils.basic.unpack_seqdim(x, B0)
    
    imgs = __p0(imgs)
    rots = __p0(rots)
    trans = __p0(trans)
    intrins = __p0(intrins)
    pts0 = __p0(pts0)
    extra0 = __p0(extra0)
    pts = __p0(pts)
    extra = __p0(extra)
    lrtlist_velo = __p0(lrtlist_velo)
    vislist = __p0(vislist)
    tidlist = __p0(tidlist)
    scorelist = __p0(scorelist)
    seg_bev_g = __p0(seg_bev_g)
    valid_bev_g = __p0(valid_bev_g)
    center_bev_g = __p0(center_bev_g)
    offset_bev_g = __p0(offset_bev_g)
    radar_data = __p0(radar_data)

    origin_T_velo0t = egopose.to(device) # B,T,4,4
    
    lrtlist_velo = lrtlist_velo.to(device)
    scorelist = scorelist.to(device)

    rgb_camXs = imgs.float().to(device)
    rgb_camXs = rgb_camXs - 0.5 # go to -0.5, 0.5
    
    seg_bev_g = seg_bev_g.to(device)
    valid_bev_g = valid_bev_g.to(device)
    center_bev_g = center_bev_g.to(device)
    offset_bev_g = offset_bev_g.to(device)

    xyz_velo0 = pts.to(device).permute(0, 2, 1)
    rad_data = radar_data.to(device).permute(0, 2, 1) # B, R, 19
    xyz_rad = rad_data[:,:,:3]
    meta_rad = rad_data[:,:,3:]

    B, S, C, H, W = rgb_camXs.shape
    B, V, D = xyz_velo0.shape

    __p = lambda x: utils.basic.pack_seqdim(x, B)
    __u = lambda x: utils.basic.unpack_seqdim(x, B)
    
    mag = torch.norm(xyz_velo0, dim=2)
    xyz_velo0 = xyz_velo0[:,mag[0]>1]
    xyz_velo0_bak = xyz_velo0.clone()

    intrins_ = __p(intrins)
    pix_T_cams_ = utils.geom.merge_intrinsics(*utils.geom.split_intrinsics(intrins_)).to(device)
    pix_T_cams = __u(pix_T_cams_)

    velo_T_cams = utils.geom.merge_rtlist(rots, trans).to(device)
    cams_T_velo = __u(utils.geom.safe_inverse(__p(velo_T_cams)))
    
    cam0_T_camXs = utils.geom.get_camM_T_camXs(velo_T_cams, ind=0)
    camXs_T_cam0 = __u(utils.geom.safe_inverse(__p(cam0_T_camXs)))
    cam0_T_camXs_ = __p(cam0_T_camXs)
    camXs_T_cam0_ = __p(camXs_T_cam0)
    
    xyz_cam0 = utils.geom.apply_4x4(cams_T_velo[:,0], xyz_velo0)
    rad_xyz_cam0 = utils.geom.apply_4x4(cams_T_velo[:,0], xyz_rad)
    
    lrtlist_cam0 = utils.geom.apply_4x4_to_lrtlist(cams_T_velo[:,0], lrtlist_velo)

    vox_util = utils.vox.Vox_util(
        Z, Y, X,
        scene_centroid=scene_centroid.to(device),
        bounds=bounds,
        assert_cube=False)

    V = xyz_velo0.shape[1]

    occ_mem0 = vox_util.voxelize_xyz(xyz_cam0, Z, Y, X, assert_cube=False)
    rad_occ_mem0 = vox_util.voxelize_xyz(rad_xyz_cam0, Z, Y, X, assert_cube=False)
    metarad_occ_mem0 = vox_util.voxelize_xyz_and_feats(rad_xyz_cam0, meta_rad, Z, Y, X, assert_cube=False)

    if model.module.use_lidar:
        rad_occ_mem0 = occ_mem0

    if not (model.module.use_radar or model.module.use_lidar):
        rad_occ_mem0 = None

    velo_T_cam0t = __u0(velo_T_cams[:,0])
    cam0_T_velo0t = __u0(utils.geom.safe_inverse(__p0(velo_T_cam0t)))

    origin_T_velo0t_ = __p0(origin_T_velo0t)
    velo_T_cam0t_ = __p0(velo_T_cam0t)
    cam0_T_velo0t_ = __p0(cam0_T_velo0t)

    origin_T_cam0t = __u0(utils.basic.matmul2(origin_T_velo0t_, velo_T_cam0t_))
    # this tells us how to get from the current timestep (I) to the last timestep (T)
    camTt_T_camIt = utils.geom.get_camM_T_camXs(origin_T_cam0t, ind=T-1) # B0,T,4,4
    camTt_T_camIt_ = __p0(camTt_T_camIt) # B0*T,4,4 = B,4,4
    camT0_T_camXs = cam0_T_camXs

    lrtlist_cam0_g = lrtlist_cam0

    rgb_camXs = __u0(rgb_camXs) # (B0, T, S, C, H, W)
    pix_T_cams = __u0(pix_T_cams) # (B0, T, S, 4, 4)
    camT0_T_camXs = __u0(camT0_T_camXs) # (B0, T, S, 4, 4)
    feat_bev_e = []
    seg_bev_e = []
    center_bev_e = []
    offset_bev_e = []
    seg_bev_gt = __u0(seg_bev_g)
    center_bev_gt = __u0(center_bev_g)
    offset_bev_gt = __u0(offset_bev_g)
    valid_bev_gt = __u0(valid_bev_g)

    if model.module.use_radar or model.module.use_lidar:
        rad_occ_mem0 = __u0(rad_occ_mem0)
        metarad_occ_mem0 = __u0(metarad_occ_mem0)

    # run all timesteps
    offset_losses = []
    offset_uncertainty_losses = []
    ce_losses = []
    ce_uncertainty_losses = []
    center_losses = []
    center_uncertainty_losses = []
    total_losses = []
    ious = []
    ious_all = []
    for t in range(T):
        # forward from rgbs up
        in_rad_occ_mem0 = None
        if model.module.use_radar:
            if not model.module.use_metaradar:
                in_rad_occ_mem0 = rad_occ_mem0[:, t]
            else:
                in_rad_occ_mem0 = metarad_occ_mem0[:, t]
        elif model.module.use_lidar:
            in_rad_occ_mem0 = rad_occ_mem0[:, t]
        _, feat_bev_et, seg_bev_et, center_bev_et, offset_bev_et = model(
                rgb_camXs=rgb_camXs[:, t],
                pix_T_cams=pix_T_cams[:, t],
                cam0_T_camXs=camT0_T_camXs[:, t],
                vox_util=vox_util,
                rad_occ_mem0=in_rad_occ_mem0)
        feat_bev_e.append(feat_bev_et.detach())
        seg_bev_e.append(seg_bev_et.detach())
        center_bev_e.append(center_bev_et.detach())
        offset_bev_e.append(offset_bev_et.detach())

        total_loss_t = torch.tensor(0.0, requires_grad=True).to(device)

        offset_loss_t = torch.abs(offset_bev_et-offset_bev_gt[:,t]).sum(dim=1, keepdim=True)
        offset_loss_t = utils.basic.reduce_masked_mean(offset_loss_t, seg_bev_gt[:,t]*valid_bev_gt[:,t])
        ce_loss_t = loss_fn(seg_bev_et, seg_bev_gt[:,t], valid_bev_gt[:,t])
        center_loss_t = balanced_mse_loss(center_bev_et, center_bev_gt[:,t])

        ce_factor = 1 / torch.exp(model.module.ce_weight)
        ce_loss_t = 10.0 * ce_loss_t * ce_factor
        ce_uncertainty_loss_t = 0.5 * model.module.ce_weight

        center_factor = 1 / (2*torch.exp(model.module.center_weight))
        center_loss_t = center_factor * center_loss_t
        center_uncertainty_loss_t = 0.5 * model.module.center_weight

        offset_factor = 1 / (2*torch.exp(model.module.offset_weight))
        offset_loss_t = offset_factor * offset_loss_t
        offset_uncertainty_loss_t = 0.5 * model.module.offset_weight

        offset_losses.append(offset_loss_t.item())
        ce_losses.append(ce_loss_t.item())
        center_losses.append(center_loss_t.item())
        ce_uncertainty_losses.append(ce_uncertainty_loss_t.item())
        center_uncertainty_losses.append(center_uncertainty_loss_t.item())
        offset_uncertainty_losses.append(offset_uncertainty_loss_t.item())

        total_loss_t += ce_uncertainty_loss_t
        total_loss_t += center_uncertainty_loss_t
        total_loss_t += offset_uncertainty_loss_t
        total_loss_t += ce_loss_t
        total_loss_t += center_loss_t
        total_loss_t += offset_loss_t
        total_losses.append(total_loss_t.item())

    feat_bev_e = torch.flatten(torch.stack(feat_bev_e, dim=1).cpu(), 0,1)
    seg_bev_e = torch.flatten(torch.stack(seg_bev_e, dim=1), 0,1)
    center_bev_e = torch.flatten(torch.stack(center_bev_e, dim=1), 0,1)
    offset_bev_e = torch.flatten(torch.stack(offset_bev_e, dim=1), 0,1)

    occ_mem0_g = occ_mem0
    feat_bev_et = __u0(feat_bev_e)
    seg_bev_et = __u0(seg_bev_e)
    center_bev_et = __u0(center_bev_e)
    offset_bev_et = __u0(offset_bev_e)

    seg_bev_e_round = torch.sigmoid(seg_bev_e).round()
    intersection = (seg_bev_e_round*seg_bev_g*valid_bev_g).sum()
    union = ((seg_bev_e_round+seg_bev_g)*valid_bev_g).clamp(0,1).sum()
    if union > 0:
        iou = intersection/union
    else:
        iou = intersection*0

    metrics['iou'] = iou.item()
    metrics['intersection'] = intersection.item()
    metrics['union'] = union.item()
    metrics['offset_loss'] = np.mean(offset_losses)
    metrics['ce_loss'] = np.mean(ce_losses)
    metrics['center_loss'] = np.mean(center_losses)
    metrics['ce_weight'] = model.module.ce_weight.item()
    metrics['center_weight'] = model.module.center_weight.item()
    metrics['offset_weight'] = model.module.offset_weight.item()

    total_loss = np.mean(total_losses)

    if sw is not None and sw.save_this:
        sw.summ_occ('0_inputs/occ_mem0', occ_mem0)
        if model.module.use_radar or model.module.use_lidar:
            rad_occ_mem0 = __p0(rad_occ_mem0)
            sw.summ_occ('0_inputs/rad_occ_mem0', rad_occ_mem0)
        for i in range(S):
            sw.summ_rgb('0_inputs/rgb_view{0}'.format(i), rgb_camXs[:,0,i]) 

        vis = []
        for t in range(T):
            vis.append(sw.summ_lrtlist_bev('', occ_mem0_g[t:t+1], lrtlist_cam0_g[t:t+1], scorelist[t:t+1], tidlist[t:t+1],
                                           vox_util, show_ids=True, only_return=True))
        sw.summ_rgbs('0_inputs/lrtlist_mem0', vis, frame_ids=list(range(T)))

        sw.summ_oned('2_outputs/seg_bev_g', seg_bev_g * (0.5+valid_bev_g*0.5), norm=False)
        sw.summ_oned('2_outputs/valid_bev_g', valid_bev_g, norm=False)
        sw.summ_oned('2_outputs/seg_bev_e', torch.sigmoid(seg_bev_e).round(), norm=False)
        sw.summ_oned('2_outputs/seg_bev_e_soft', torch.sigmoid(seg_bev_e), norm=False)

        sw.summ_oned('2_outputs/center_bev_g', center_bev_g, norm=False)
        sw.summ_oned('2_outputs/center_bev_e', center_bev_e, norm=False)

        sw.summ_flow('2_outputs/offset_bev_e', offset_bev_e, clip=10)
        sw.summ_flow('2_outputs/offset_bev_g', offset_bev_g, clip=10)

    return total_loss, metrics
    
def main(
    exp_name='eval',
    # val
    log_freq=100,
    shuffle=False,
    dset='trainval',
    batch_size=40,
    eff_batch_size=8,
    nworkers=6,
    # data/log/load directories
    data_dir='/home/scratch/zhaoyuaf/nuscenes/',
    log_dir='logs_eval_nuscenes_bevseg',
    init_dir='/zfsauton2/home/zhaoyuaf/map3d/checkpoints/sb_res101',
    ignore_load=None,
    # data
    resolution_scale=2,
    rand_flip=False,
    ncams=6,
    nsweeps=3,
    # model
    encoder_type='res101',
    use_radar=False,
    use_lidar=False,
    use_metaradar=False,
    do_rgbcompress=True,
    # cuda
    device='cuda:7',
    device_ids=[7,4,5,6],
    ):

    B = batch_size
    eff_B = eff_batch_size
    eff_T = B // eff_B
    assert (B == eff_B * eff_T)
    print(eff_B, eff_T)

    ## autogen a name
    model_name = "%02d" % B
    model_name += "_%s" % exp_name
    import datetime
    model_date = datetime.datetime.now().strftime('%H:%M:%S')
    model_name = model_name + '_' + model_date
    print('model_name', model_name)

    # set up tb writer
    writer_ev = SummaryWriter(log_dir + '/' + model_name + '/ev', max_queue=10, flush_secs=60)

    # set up dataloader
    final_dim = (224 * resolution_scale, 400 * resolution_scale)
    xbound = [-50.0, 50.0, 0.5]
    ybound = [-50.0, 50.0, 0.5]
    zbound = [-5.0, 5.0, 10.0]
    dbound = [4.0, 45.0, 1.0]
    grid_conf = {
        'xbound': xbound,
        'ybound': ybound,
        'zbound': zbound,
        'dbound': dbound,
    }
    data_aug_conf = {
        'final_dim': final_dim,
        'H': 900, 'W': 1600,
        'rand_flip': rand_flip,
        'cams': ['CAM_FRONT_LEFT', 'CAM_FRONT', 'CAM_FRONT_RIGHT',
                 'CAM_BACK_LEFT', 'CAM_BACK', 'CAM_BACK_RIGHT'],
        'Ncams': ncams,
    }
    _, val_dataloader = compile_data(
        dset, data_dir, data_aug_conf=data_aug_conf,
        grid_conf=grid_conf, bsz=B, nworkers=nworkers,
        parser_name='vizdata',
        shuffle=shuffle,
        seqlen=1,
        nsweeps=nsweeps,
        get_tids=True,
        nworkers_val=nworkers,
    )
    val_iterloader = iter(val_dataloader)

    max_iters = len(val_dataloader)

    # set up model & seg loss
    seg_loss_fn = SimpleLoss(2.13).to(device)
    model = Segnet(Z, Y, X, use_radar=use_radar, use_lidar=use_lidar, use_metaradar=use_metaradar, do_rgbcompress=do_rgbcompress, encoder_type=encoder_type)
    model = model.to(device)
    model = torch.nn.DataParallel(model, device_ids=device_ids)
    parameters = list(model.parameters())
    total_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print('total_params', total_params)

    if init_dir:
        _ = saverloader.load(init_dir, model.module, ignore_load=ignore_load)
    global_step = 0
    requires_grad(parameters, False)
    model.eval()

    # logging pools. pool size should be larger than max_iters
    n_pool = 10000
    loss_pool_ev = utils.misc.SimplePool(n_pool, version='np')
    time_pool_ev = utils.misc.SimplePool(n_pool, version='np')
    ce_pool_ev = utils.misc.SimplePool(n_pool, version='np')
    center_pool_ev = utils.misc.SimplePool(n_pool, version='np')
    offset_pool_ev = utils.misc.SimplePool(n_pool, version='np')

    intersection = 0
    union = 0
    while global_step < max_iters:
        # read sample
        read_start_time = time.time()
        global_step += 1

        sw_ev = utils.improc.Summ_writer(
            writer=writer_ev,
            global_step=global_step,
            log_freq=log_freq,
            fps=2,
            scalar_freq=int(log_freq/2),
            just_gif=True)
        sw_ev.save_this = False
        
        try:
            sample = next(val_iterloader)
        except StopIteration:
            break

        read_time = time.time()-read_start_time

        # run val iteration
        iter_start_time = time.time()
            
        with torch.no_grad():
            total_loss, metrics = run_model(model, seg_loss_fn, sample, eff_B, eff_T, device, sw_ev)

        intersection += metrics['intersection']
        union += metrics['union']

        sw_ev.summ_scalar('pooled/iou_ev', intersection/union)
        
        loss_pool_ev.update([total_loss])
        sw_ev.summ_scalar('pooled/total_loss', loss_pool_ev.mean())
        sw_ev.summ_scalar('stats/total_loss', total_loss)

        ce_pool_ev.update([metrics['ce_loss']])
        sw_ev.summ_scalar('pooled/ce_loss', ce_pool_ev.mean())
        sw_ev.summ_scalar('stats/ce_loss', metrics['ce_loss'])
        
        center_pool_ev.update([metrics['center_loss']])
        sw_ev.summ_scalar('pooled/center_loss', center_pool_ev.mean())
        sw_ev.summ_scalar('stats/center_loss', metrics['center_loss'])

        offset_pool_ev.update([metrics['offset_loss']])
        sw_ev.summ_scalar('pooled/offset_loss', offset_pool_ev.mean())
        sw_ev.summ_scalar('stats/offset_loss', metrics['offset_loss'])

        iter_time = time.time()-iter_start_time

        time_pool_ev.update([iter_time])
        sw_ev.summ_scalar('pooled/time_per_batch', time_pool_ev.mean())
        sw_ev.summ_scalar('pooled/time_per_el', time_pool_ev.mean()/float(B))

        print('%s; step %06d/%d; rtime %.2f; itime %.2f; loss %.5f; iou_ev %.1f' % (
            model_name, global_step, max_iters, read_time, iter_time,
            total_loss.item(), 100*intersection/union))
    print('final %s mean iou' % dset, 100*intersection/union)
    
    writer_ev.close()
            

if __name__ == '__main__':
    Fire(main)

