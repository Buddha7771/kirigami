import os
import pathlib
import argparse
from multipledispatch import dispatch
import torch
from tqdm import tqdm
from nn.Embedding import BpseqEmbedding

@dispatch(argparse.Namespace)
def embed(conf) -> None:
    return embed(conf.in_list, conf.out_directory)

@dispatch(pathlib.Path, pathlib.Path)
def embed(in_list, out_directory) -> None:
    '''Embeds .bpseq files as `torch.Tensors`'''
    os.path.exists(out_directory) or os.mkdir(out_directory)
    bp = BpseqEmbedding()
    with open(in_list, 'r') as f:
        in_files = f.read().splitlines()

    out_files = []
    for file in tqdm(in_files):
        with open(file, 'r') as f:
            txt = f.read()
        embed = bp(txt)
        base_name = os.path.basename(file)
        base_name, _ = os.path.splitext(base_name)
        out_file = os.path.join(out_directory, base_name + '.pt')
        out_files.append(out_file)
        torch.save(embed, out_file)

    out_list = os.path.join(out_directory, 'out.lst')
    with open(out_list, 'w') as f:
        for out_file in out_files:
            f.write(out_file + '\n')
