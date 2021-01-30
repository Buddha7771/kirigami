from argparse import Namespace
from pathlib import Path
import os
from tqdm import tqdm
import torch
from torch import nn
from torch.utils.data import DataLoader
from kirigami.utils.data import FastaDataset
from kirigami.utils.utilities import path2munch


__all__ = ['predict']


def predict(args: Namespace) -> None:
    '''Evaluates model from config file'''
    config = path2munch(args.config)

    try:
        saved = torch.load(config.training.best)
    except os.path.exists(config.training.checkpoint):
        saved = torch.load(config.training.checkpoint)
    else:
        raise FileNotFoundError('Can\'t find checkpoint files')

    model = MainNet(config.model)
    model.load_state_dict(saved['model_state_dict'])
    model.eval()

    out_files = []
    with open(config.in_file, 'r') as f:
        in_files = f.read().splitlines()
        for file in in_files:
            file = os.path.basename(file)
            file, _ = os.path.splitext(file)
            file += '.bpseq'
            file = os.path.join(args.out_directory, file)
            out_files.append(file)

    dataset = FastaDataset(args.in_file, args.quiet)
    loop_zip = zip(out_files, dataset)
    loop = loop_zip if args.quiet else tqdm(loop_zip)

    for out_file, sequence in loop:
        pred = model(sequence)
        pred = binarize(pred)
        bpseq_str = tensor2bpseq(sequence, pred)
        with open(out_file, 'w') as f:
            f.write(bpseq_str)
