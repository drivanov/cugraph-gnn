# Copyright (c) 2024-2025, NVIDIA CORPORATION.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# This example illustrates link classification using the ogbl-wikikg2 dataset.

import os
import argparse
import warnings

from typing import Tuple, Any

import numpy

import torch

import torch.nn.functional as F
from torch.nn import Parameter
from torch_geometric.nn import FastRGCNConv, GAE
from torch.nn.parallel import DistributedDataParallel

import torch_geometric

from pylibwholegraph.torch.initialize import (
    init as wm_init,
    finalize as wm_finalize,
)

# Ensures that a CUDA context is not created on import of rapids.
# Allows pytorch to create the context instead
os.environ["RAPIDS_NO_INITIALIZE"] = "1"


def init_pytorch_worker(rank, world_size, uid):
    import rmm

    rmm.reinitialize(devices=[rank], pool_allocator=True, managed_memory=True)

    import cupy
    from rmm.allocators.cupy import rmm_cupy_allocator

    cupy.cuda.set_allocator(rmm_cupy_allocator)

    from cugraph.gnn import cugraph_comms_init

    cugraph_comms_init(
        rank,
        world_size,
        uid,
        rank,
    )

    wm_init(rank, world_size, rank, world_size)

    os.environ["MASTER_ADDR"] = "localhost"
    os.environ["MASTER_PORT"] = "12355"
    torch.distributed.init_process_group(
        "nccl",
        rank=rank,
        world_size=world_size,
    )

    from cugraph.testing.mg_utils import enable_spilling

    enable_spilling()


class RGCNEncoder(torch.nn.Module):
    def __init__(self, num_nodes, hidden_channels, num_relations, num_bases=30):
        super().__init__()
        self.node_emb = Parameter(torch.empty(num_nodes, hidden_channels))
        self.conv1 = FastRGCNConv(
            hidden_channels, hidden_channels, num_relations, num_bases=num_bases
        )
        self.conv2 = FastRGCNConv(
            hidden_channels, hidden_channels, num_relations, num_bases=num_bases
        )
        self.reset_parameters()

    def reset_parameters(self):
        torch.nn.init.xavier_uniform_(self.node_emb)
        self.conv1.reset_parameters()
        self.conv2.reset_parameters()

    def forward(self, edge_index, edge_type):
        x = self.node_emb
        x = self.conv1(x, edge_index, edge_type).relu_()
        x = F.dropout(x, p=0.2, training=self.training)
        x = self.conv2(x, edge_index, edge_type)
        return x


def load_data(
    rank: int,
    world_size: int,
    data: Any,
) -> Tuple[
    Tuple["torch_geometric.data.FeatureStore", "torch_geometric.data.GraphStore"],
    "torch_geometric.data.FeatureStore",
]:
    from cugraph_pyg.data import GraphStore, WholeFeatureStore, TensorDictFeatureStore

    graph_store = GraphStore()
    feature_store = TensorDictFeatureStore()  # empty fs required by PyG
    edge_feature_store = WholeFeatureStore()

    print("num nodes:", data.num_nodes)

    graph_store[
        ("n", "e", "n"), "coo", False, (data.num_nodes, data.num_nodes)
    ] = torch.tensor_split(data.edge_index.cuda(), world_size, dim=1)[rank]

    edge_feature_store[("n", "e", "n"), "rel", None] = torch.tensor_split(
        data.edge_reltype.cuda(),
        world_size,
    )[rank]

    return (feature_store, graph_store), edge_feature_store


def train(epoch, model, optimizer, train_loader, edge_feature_store, num_steps=None):
    model.train()
    optimizer.zero_grad()

    for i, batch in enumerate(train_loader):
        r = edge_feature_store[("n", "e", "n"), "rel"][batch.e_id].flatten().cuda()
        z = model.encode(batch.edge_index, r)

        loss = model.recon_loss(z, batch.edge_index)
        loss.backward()
        optimizer.step()

        if i % 10 == 0:
            print(
                f"Epoch: {epoch:02d}, Iteration: {i:02d}, Loss: {loss:.4f}", flush=True
            )
        if num_steps and i == num_steps:
            break


def test(stage, epoch, model, loader, num_steps=None):
    # TODO support ROC-AUC metric
    # Predict probabilities of future edges
    model.eval()

    rr = 0.0
    for i, (h, h_neg, t, t_neg, r) in enumerate(loader):
        if num_steps and i >= num_steps:
            break

        ei = torch.concatenate(
            [
                torch.stack([h, t]).cuda(),
                torch.stack([h_neg.flatten(), t_neg.flatten()]).cuda(),
            ],
            dim=-1,
        )

        r = torch.concatenate([r, torch.repeat_interleave(r, h_neg.shape[-1])]).cuda()

        z = model.encode(ei, r)
        q = model.decode(z, ei)

        _, ix = torch.sort(q, descending=True)
        rr += 1.0 / (1.0 + ix[0])

    print(f"epoch {epoch:02d} {stage} mrr:", rr / i, flush=True)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hidden_channels", type=int, default=128)
    parser.add_argument("--num_layers", type=int, default=1)
    parser.add_argument("--lr", type=float, default=0.001)
    parser.add_argument("--epochs", type=int, default=4)
    parser.add_argument("--batch_size", type=int, default=16384)
    parser.add_argument("--num_neg", type=int, default=500)
    parser.add_argument("--num_pos", type=int, default=-1)
    parser.add_argument("--fan_out", type=int, default=10)
    parser.add_argument("--dataset", type=str, default="ogbl-wikikg2")
    parser.add_argument("--dataset_root", type=str, default="datasets")
    parser.add_argument("--seeds_per_call", type=int, default=-1)
    parser.add_argument("--n_devices", type=int, default=-1)

    return parser.parse_args()


def run_train(rank, world_size, uid, model, data, meta, splits, args):
    init_pytorch_worker(
        rank,
        world_size,
        uid,
    )

    model = model.to(rank)
    model = GAE(DistributedDataParallel(model, device_ids=[rank]))
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)

    data, edge_feature_store = load_data(rank, world_size, data)

    eli = torch.stack(
        [
            torch.tensor_split(splits["train"]["head"], world_size)[rank],
            torch.tensor_split(splits["train"]["tail"], world_size)[rank],
        ]
    )

    from cugraph_pyg.loader import LinkNeighborLoader

    train_loader = LinkNeighborLoader(
        data,
        [args.fan_out] * args.num_layers,
        edge_label_index=eli,
        local_seeds_per_call=args.seeds_per_call if args.seeds_per_call > 0 else None,
        batch_size=args.batch_size,
        shuffle=True,
        drop_last=True,
    )

    def get_eval_loader(stage: str):
        head = torch.tensor_split(splits[stage]["head"], world_size)[rank]
        tail = torch.tensor_split(splits[stage]["tail"], world_size)[rank]

        head_neg = torch.tensor_split(
            splits[stage]["head_neg"][:, : args.num_neg], world_size
        )[rank]
        tail_neg = torch.tensor_split(
            splits[stage]["tail_neg"][:, : args.num_neg], world_size
        )[rank]

        rel = torch.tensor_split(splits[stage]["relation"], world_size)[rank]

        return torch.utils.data.DataLoader(
            torch.utils.data.TensorDataset(
                head.pin_memory(),
                head_neg.pin_memory(),
                tail.pin_memory(),
                tail_neg.pin_memory(),
                rel.pin_memory(),
            ),
            batch_size=1,
            shuffle=False,
            drop_last=True,
        )

    test_loader = get_eval_loader("test")
    valid_loader = get_eval_loader("valid")

    num_train_steps = (args.num_pos // args.batch_size) if args.num_pos > 0 else 100

    for epoch in range(1, 1 + args.epochs):
        train(
            epoch,
            model,
            optimizer,
            train_loader,
            edge_feature_store,
            num_steps=num_train_steps,
        )
        test("validation", epoch, model, valid_loader, num_steps=1024)

    test("test", epoch, model, test_loader, num_steps=1024)

    wm_finalize()
    from cugraph.gnn import cugraph_comms_shutdown

    cugraph_comms_shutdown()


if __name__ == "__main__":
    if os.getenv("CI", "false").lower() == "true":
        warnings.warn("Skipping SMNG example in CI due to memory limit")
    else:
        args = parse_args()

        # change the allocator before any allocations are made
        from rmm.allocators.torch import rmm_torch_allocator

        torch.cuda.memory.change_current_allocator(rmm_torch_allocator)

        # import ogb here to stop it from creating a context and breaking pytorch/rmm
        from ogb.linkproppred import PygLinkPropPredDataset

        with torch.serialization.safe_globals(
            [
                torch_geometric.data.data.DataEdgeAttr,
                torch_geometric.data.data.DataTensorAttr,
                torch_geometric.data.storage.GlobalStorage,
                numpy.core.multiarray._reconstruct,
                numpy.ndarray,
                numpy.dtype,
                numpy.dtypes.Int64DType,
            ]
        ):
            data = PygLinkPropPredDataset(args.dataset, root=args.dataset_root)
            dataset = data[0]

            splits = data.get_edge_split()

        meta = {}
        meta["num_nodes"] = dataset.num_nodes
        meta["num_rels"] = dataset.edge_reltype.max() + 1

        model = RGCNEncoder(
            meta["num_nodes"],
            hidden_channels=args.hidden_channels,
            num_relations=meta["num_rels"],
        )

        print("Data =", data)
        if args.n_devices == -1:
            world_size = torch.cuda.device_count()
        else:
            world_size = args.n_devices
        print("Using", world_size, "GPUs...")

        from cugraph.gnn import cugraph_comms_create_unique_id

        uid = cugraph_comms_create_unique_id()
        torch.multiprocessing.spawn(
            run_train,
            (world_size, uid, model, data, meta, splits, args),
            nprocs=world_size,
            join=True,
        )
