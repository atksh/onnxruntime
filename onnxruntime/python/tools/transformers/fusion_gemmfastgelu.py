# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation.  All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------

from logging import getLogger

from fusion_base import Fusion
from fusion_utils import NumpyHelper
from onnx import helper
from onnx_model import OnnxModel

logger = getLogger(__name__)


class FusionGemmFastGelu(Fusion):
    def __init__(self, model: OnnxModel):
        super().__init__(model, "GemmFastGelu", "FastGelu", "GemmFastGelu")
    
    def fuse(self, node, input_name_to_nodes, output_name_to_node):
        has_bias = False
        if len(node.input) == 2:
            has_bias = True

        [matmul] = self.model.match_parent_path(node, ["MatMul"], [0])
        if matmul is None:
            return

        weight = None
        # matmul weight should be two dimension
        weight_index = -1
        for i, input in enumerate(matmul.input):
            initializer = self.model.get_initializer(input)
            if initializer is None:
                continue
            weight_index = i
            weight = NumpyHelper.to_array(initializer)
            break
        if weight is None:
            return
        if len(weight.shape) != 2:
            return

        if has_bias:
            bias_weight = None
            # bias weight should be one dimension
            bias_index = -1
            for i, input in enumerate(node.input):
                initializer = self.model.get_initializer(input)
                if initializer is None:
                    continue
                bias_index = i
                bias_weight = NumpyHelper.to_array(initializer)
                break
            if bias_weight is None:
                return
            if len(bias_weight.shape) != 1:
                return

        subgraph_nodes = [node, matmul]
        if not self.model.is_safe_to_fuse_nodes(
            subgraph_nodes, [node.output[0]], input_name_to_nodes, output_name_to_node
        ):
            return

        self.nodes_to_remove.extend(subgraph_nodes)

        inputs=[matmul.input[1-weight_index], matmul.input[weight_index], node.input[bias_index]] if has_bias else [matmul.input[1-weight_index], matmul.input[weight_index]]
        fused_node = helper.make_node(
            "GemmFastGelu",
            inputs=inputs,
            outputs=node.output,
            name=self.model.create_node_name("GemmFastGelu"),
        )
        fused_node.domain = "com.microsoft"
        self.nodes_to_add.append(fused_node)
        self.node_name_to_graph_name[fused_node.name] = self.this_graph_name
