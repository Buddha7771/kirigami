import os
import sys
import json
import itertools
from functools import reduce
import time
import logging
import datetime
from pathlib import Path
from tqdm import tqdm
from munch import munchify, Munch
from dataclasses import dataclass

import torch
from torch import nn
from torch.utils.data import DataLoader, Dataset
from torch.cuda.amp import GradScaler, autocast
from torch.utils.checkpoint import checkpoint
import torch.multiprocessing as mp
from torch.utils.checkpoint import checkpoint_sequential
from torch.nn.functional import binary_cross_entropy

import kirigami.nn
from kirigami.nn.utils import *
from kirigami.utils import binarize


PAIRS = {"AU", "UA", "CG", "GC", "GU", "UG"}


def concat(fasta):
    out = fasta.unsqueeze(-1)
    out = torch.cat(out.shape[-2] * [out], dim=-1)
    out_t = out.transpose(-1, -2)
    out = torch.cat([out, out_t], dim=-3)
    return out


def to_str(ipt):
    ipt_ = ipt.squeeze()
    total_length = ipt_.shape[1]
    fasta_length = int(ipt_.sum().item())
    beg = (total_length - fasta_length) // 2
    end = beg + fasta_length
    _, js = torch.max(ipt_[:,beg:end], 0)
    return "".join("ACGU"[j] for j in js)


def to_pairs(ipt, seq_len):
    beg = (MAX_SIZE - seq_len) // 2
    end = beg + seq_len
    ipt = ipt[beg:end, beg:end]
    vals, idxs = torch.max(ipt, 0)
    grd_set = set()
    for i, (val, idx) in enumerate(zip(vals, idxs)):
        if val == 1 and i < idx:
            grd_set.add((i, idx.item()))
    return grd_set


MAX_SIZE = 512


def get_scores(prd_pairs, grd_pairs, seq_len):
    total = seq_len * (seq_len-1) / 2
    n_prd, n_grd = len(prd_pairs), len(grd_pairs)
    tp = float(len(prd_pairs.intersection(grd_pairs)))
    fp = len(prd_pairs) - tp
    fn = len(grd_pairs) - tp
    tn = total - tp - fp - fn
    mcc = f1 = 0. 
    if n_prd > 0 and n_grd > 0:
        sn = tp / (tp+fn)
        pr = tp / (tp+fp)
        if tp > 0:
            f1 = 2*sn*pr / (pr+sn)
        if (tp+fp) * (tp+fn) * (tn+fp) * (tn+fn) > 0:
            mcc = (tp*tn-fp*fn) / ((tp+fp)*(tp+fn)*(tn+fp)*(tn+fn))**.5
    return {"tp": tp,
            "tn": tn,
            "fp": fp,
            "fn": fn,
            "f1": f1,
            "mcc": mcc,
            "n_grd": n_grd,
            "n_prd": n_prd}


def binarize(lab_,
             seq: str,
             thres_pairs: int,
             thres_prob: float,
             min_dist: int = 4,
             symmetrize: bool = True,
             canonicalize: bool = True):

    full_len = lab_.shape[1]
    seq_len = len(seq) 
    beg = (full_len - seq_len) // 2
    end = beg + seq_len
    lab = lab_.squeeze()[beg:end, beg:end]
    if symmetrize:
        lab = lab + lab.T
    lab = lab.ravel()

    # get 1D indices of sorted probs
    idxs = lab.argsort(descending=True)
    # convert 1D indices back to 2D
    ii = torch.div(idxs, seq_len, rounding_mode="floor")
    jj = (idxs % seq_len)
    # filter idxs that are too close
    mask = abs(ii-jj) >= min_dist
    ii = ii[mask].tolist()
    jj = jj[mask].tolist()

    # return `thres_pairs` number of idxs
    kept = torch.zeros(seq_len, dtype=bool)
    pair_idx = out_count = 0 
    out_set = set()
    out_tensor = torch.zeros_like(lab_)
    
    while out_count < thres_pairs and pair_idx < len(ii):
        i, j = ii[pair_idx], jj[pair_idx]
        pair_idx += 1
        if kept[i] or kept[j] or (canonicalize and seq[i]+seq[j] not in PAIRS):
        # if canonicalize and seq[i]+seq[j] not in PAIRS:
            continue
        out_set.add(tuple(sorted((i, j))))
        # out_set.add((j, i))
        kept[i] = kept[j] = True
        out_tensor[i+beg, j+beg] = out_tensor[j+beg, i+beg] = 1
        out_count += 1
    
    return out_tensor, out_set



def main():
    with open(sys.argv[1], "r") as f:
        txt = f.read()
        p = munchify(json.loads(txt))

    logging.basicConfig(format="%(asctime)s\n%(message)s",
                        stream=sys.stdout,
                        level=logging.INFO)
    logging.info("Run with config:\n"+txt) 

    # stores "global" variables (objects) for training
    g = Munch()

    ####### construct all objects for training ####### 

    DEVICE = torch.device(p.device)

    # build model from `Module`'s
    module_list = [eval(layer) for layer in p.layers]
    module_list = sequentialize(module_list)
    model = nn.Sequential(*module_list)
    model = model.to(DEVICE)
    N = torch.cuda.device_count()
    if model == torch.device("cuda") and N > 1:
        model = nn.DataParallel(model, output_device=[1])

    # load data and copy into `DataLoader`'s
    tr_set = torch.load(p.tr_set)
    tr_set.tensors = [tensor.to(DEVICE) for tensor in tr_set.tensors]
    vl_set = torch.load(p.vl_set)
    vl_set.tensors = [tensor.to(DEVICE) for tensor in vl_set.tensors]

    g.MODEL = model
    g.DEVICE = DEVICE
    g.BEST_MCC = -float("inf")
    g.BEST_LOSS = float("inf") 
    g.CRIT = eval(p.criterion)()
    g.OPT = eval(p.optimizer)(g.MODEL.parameters(), lr=p.lr)
    g.SCALER = GradScaler()
    g.TR_LOADER = DataLoader(tr_set, shuffle=True, batch_size=p.batch_size)
    g.TR_LEN = len(tr_set)
    g.VL_LOADER = DataLoader(vl_set, shuffle=False, batch_size=1)
    g.VL_LEN = len(vl_set)

    ####### main training (and validation) loop ####### 

    for epoch in range(p.epochs):
        start = datetime.datetime.now()
        logging.info(f"Beginning epoch {epoch}")
        loss_tot = 0.

        for i, batch in enumerate(tqdm(g.TR_LOADER, disable=not p.bar)):

            fasta, thermo, con = batch
            fasta = fasta.to_dense().float()
            con = con.to_dense().float().reshape(-1, MAX_SIZE, MAX_SIZE)
            ipt = concat(fasta)
            if p.thermo:
                thermo = thermo.to_dense()
                thermo = thermo.unsqueeze(1)
                ipt = torch.cat((thermo, ipt), 1)

            with autocast(enabled=p.mix_prec):
                if p.chkpt_seg > 0:
                    pred = checkpoint_sequential(g.MODEL, p.chkpt_seg, ipt)
                else:
                    pred = g.MODEL(ipt)
                con = con.reshape_as(pred)
                loss = g.CRIT(pred, con)
                loss /= p.iter_acc
            g.SCALER.scale(loss).backward()

            if i % p.iter_acc == 0:
                g.SCALER.step(g.OPT)
                g.SCALER.update()
                g.OPT.zero_grad(set_to_none=True)
            loss_tot += loss.item()

        loss_avg = loss_tot / len(g.TR_LOADER) 
        torch.save({"epoch": epoch,
                    "model_state_dict": g.MODEL.state_dict(),
                    "optimizer_state_dict": g.OPT.state_dict(),
                    "loss": loss_avg},
                   p.tr_chk)
        
        end = datetime.datetime.now()
        delta = end - start
        mess = (f"Training time for epoch {epoch}: {delta.seconds}s\n" +
                f"Mean training loss for epoch {epoch}: {loss_avg}\n" +
                f"Memory allocated: {torch.cuda.memory_allocated() / 2**20} MB\n" +
                f"Memory cached: {torch.cuda.memory_cached() / 2**20} MB")
        logging.info(mess)


        ######## validation #########

        if epoch % p.eval_freq > 0:
            print("\n\n")
            continue

        start = datetime.datetime.now()
        g.MODEL.eval()

        raw_loss_mean = bin_loss_mean = mcc_mean = 0
        f1_mean = prd_pairs_mean = grd_pairs_mean = 0

        with torch.no_grad():
            for i, batch in enumerate(tqdm(g.VL_LOADER, disable=not p.bar)):
                fasta, thermo, con = batch
                fasta = fasta.to_dense()
                seq = to_str(fasta)
                con = con.to_dense().float().reshape(MAX_SIZE, MAX_SIZE)
                ipt = concat(fasta.float())
                if p.thermo:
                    thermo = thermo.to_dense()
                    thermo = thermo.unsqueeze(1)
                    ipt = torch.cat((thermo, ipt), 1)
                    
                raw_pred = g.MODEL(ipt).squeeze()
                raw_loss = g.CRIT(raw_pred, con)
                if isinstance(g.CRIT, torch.nn.BCEWithLogitsLoss):
                    raw_pred = torch.nn.functional.sigmoid(raw_pred) 

                grd_set = to_pairs(con, len(seq))
                bin_pred, prd_set = binarize(lab_=raw_pred,
                                             seq=seq,
                                             min_dist=4,
                                             thres_pairs=len(grd_set),
                                             thres_prob=0,
                                             symmetrize=p.symmetrize,
                                             canonicalize=p.canonicalize)

                if isinstance(g.CRIT, torch.nn.BCEWithLogitsLoss):
                    bin_loss = binary_cross_entropy(bin_pred, con)
                else:
                    bin_loss = g.CRIT(bin_pred, con)

                bin_scores = get_scores(prd_set, grd_set, len(seq))

                raw_loss_mean += raw_loss.item() / g.VL_LEN
                bin_loss_mean += bin_loss.item() / g.VL_LEN
                mcc_mean += bin_scores["mcc"] / g.VL_LEN
                f1_mean += bin_scores["f1"] / g.VL_LEN
                prd_pairs_mean += bin_scores["n_prd"] / g.VL_LEN
                grd_pairs_mean += bin_scores["n_grd"] / g.VL_LEN


        delta = datetime.datetime.now() - start
        mess = (f"Validation time for epoch {epoch}: {delta.seconds}s\n" +
                f"Raw mean validation loss for epoch {epoch}: {raw_loss_mean}\n" +
                f"Mean MCC for epoch {epoch}: {mcc_mean}\n" +
                f"Mean ground pairs: {grd_pairs_mean}\n" +
                f"Mean predicted pairs: {prd_pairs_mean}\n" +
                f"Binarized mean validation loss for epoch {epoch}: {bin_loss_mean}\n")
        if mcc_mean > g.BEST_MCC:
            logging.info(f"New optimum at epoch {epoch}")
            g.BEST_MCC = mcc_mean
            g.BEST_LOSS = bin_loss_mean
            torch.save({"epoch": epoch,
                        "model_state_dict": g.MODEL.state_dict(),
                        "optimizer_state_dict": g.OPT.state_dict(),
                        "grd_pairs_mean": grd_pairs_mean,
                        "prd_pairs_mean": prd_pairs_mean,
                        "mcc_mean": mcc_mean,
                        "f1_mean": f1_mean,
                        "raw_loss_mean": raw_loss_mean,
                        "bin_loss_mean": bin_loss_mean},
                       p.vl_chk)
            mess += f"*****NEW MAXIMUM MCC*****\n"
        mess += "\n\n"
        logging.info(mess)
        g.MODEL.train()


if __name__ == "__main__":
    main()
