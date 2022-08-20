# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation.  All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------
from logging import getLogger
from typing import Tuple

from numpy import array_equal, ndarray
from onnx import TensorProto, NodeProto, helper, numpy_helper
from onnx_model import OnnxModel

logger = getLogger(__name__)


class FusionUtils:
    def __init__(self, model: OnnxModel):
        self.model: OnnxModel = model

    def cast_graph_input_to_int32(self, input_name: str) -> Tuple[bool, str]:
        graph_input = self.model.find_graph_input(input_name)
        if graph_input is not None and graph_input.type.tensor_type.elem_type != TensorProto.INT32:
            cast_output, cast_node = self.cast_input_to_int32(input_name)
            logger.debug(f"Casted graph input {input_name} to int32")
            return True, cast_output

        logger.debug(f"Did not cast graph input {input_name} to int32: found {graph_input is not None}")
        return False, input_name

    def cast_input_to_int32(self, input_name: str):
        cast_output = input_name + "_int32"

        # Avoid consequent Cast nodes.
        inputs = [input_name]
        output_name_to_node = self.model.output_name_to_node()
        if input_name in output_name_to_node:
            parent_node = output_name_to_node[input_name]
            if parent_node and parent_node.op_type == "Cast":
                inputs = [parent_node.input[0]]

        cast_node = helper.make_node("Cast", inputs=inputs, outputs=[cast_output])
        cast_node.attribute.extend([helper.make_attribute("to", int(TensorProto.INT32))])
        self.model.add_node(cast_node)

        return cast_output, cast_node

    def remove_cast_int32(self, input_name: str):
        input_name_to_nodes = self.model.input_name_to_nodes()
        nodes = input_name_to_nodes[input_name]
        for node in nodes:
            if node.op_type == "Cast":
                is_int32 = False
                for att in node.attribute:
                    if att.name == "to" and att.i == int(TensorProto.INT32):
                        is_int32 = True
                        break
                if is_int32:
                    output_name = node.output[0]
                    self.model.remove_node(node)
                    self.model.replace_input_of_all_nodes(output_name, input_name)

    @staticmethod
    def check_node_attribute(node, attribute_name: str, expected_value, default_value=None):
        """Verify that a node has expected value for an attribute.

        Args:
            node (NodeProto): a node to check
            attribute_name (str): name of attribute
            expected_value (Any): expected value of the attribute
            default_value (Any, optional): default value if the attribute does not exist. Defaults to None.

        Returns:
            bool: whether the check is passed or not
        """
        value = default_value
        for attr in node.attribute:
            if attr.name == attribute_name:
                value = helper.get_attribute_value(attr)

        if isinstance(expected_value, list):
            return (isinstance(value, ndarray) or isinstance(value, list)) and array_equal(
                expected_value, value, equal_nan=False
            )
        else:
            return value == expected_value

    @staticmethod
    def check_qdq_node_for_fusion(node : NodeProto, model : OnnxModel):
        """Verify if a provided QuantizeLinear (Q) / DequantizeLinear (DQ) node is a good candidate for fusion.
           It is a good candidate for fusion if:
           (1) The Q/DQ node is for per-tensor quantization (per-axis quantization is not supported)
           (2) The Q/DQ node should have constant scale
           (3) The Q/DQ node should have a zero point of 0
        Args:
            node (NodeProto): a Q/DQ node to check
        Returns:
            bool: whether the check is passed or not
        """
        if not(node.op_type == "QuantizeLinear" or node.op_type == "DequantizeLinear"):
            logger.debug(f"Provided node is not a Q/DQ node. Op Type: {node.op_type}")

        if model.get_constant_value(node.input[1]) is None:
            return False

        # If the Q/DQ node has no zero point input, it is assumed to be 0 (per ONNX spec)
        if len(node.input) != 3:
            return True

        # Zero point should be constant and should have a value of 0
        zero_point = model.get_constant_value(node.input[2])
        if zero_point is None or zero_point != 0:
            return False
        
        return True

    def check_node_input_value(self, node, input_index: int, expected_value):
        """Verify that a node has expected input value

        Args:
            node (NodeProto): a node to check
            input_index (int): index of its input to be verified
            expected_value (Any): expected value of the input

        Returns:
            bool: whether the check is passed or not
        """
        assert len(node.input) > input_index

        value = self.model.get_constant_value(node.input[input_index])

        if isinstance(expected_value, list):
            return (isinstance(value, ndarray) or isinstance(value, list)) and array_equal(
                expected_value, value, equal_nan=False
            )
        else:
            return value == expected_value

    def get_dtype(self, shape_infer_helper, input_or_output_name: str) -> int:
        """Get data type of an input or output.

        Args:
            shape_infer_helper (SymbolicShapeInferenceHelper): object of symbolic shape inference
            input_or_output_name (str): name of input or output

        Returns:
            int: tensor data type
        """
        dtype = self.model.get_dtype(input_or_output_name)
        if dtype is not None:
            return dtype

        if shape_infer_helper:
            tensor_proto = shape_infer_helper.known_vi_[input_or_output_name]
            if tensor_proto.type.tensor_type.HasField("elem_type"):
                return tensor_proto.type.tensor_type.elem_type

        return None

    def remove_cascaded_cast_nodes(self):
        """Remove Cast node that are overrided by another Cast node like  --> Cast --> Cast -->
        Note that this shall be used carefully since it might introduce semantic change.
        For example, float -> int -> float could get different value than the original float value.
        So, it is recommended to used only in post-processing of mixed precision conversion.
        """
        removed_count = 0
        for node in self.model.nodes():
            if node.op_type == "Cast":
                parent = self.model.get_parent(node, 0)
                if parent and parent.op_type == "Cast":
                    node.input[0] = parent.input[0]
                    removed_count += 1

        if removed_count > 0:
            logger.info(f"Removed {removed_count} cascaded Cast nodes")
            self.model.prune_graph()

    def remove_identity_nodes(self):
        """Remove Identity nodes, except those right before graph output."""
        nodes_to_remove = []
        for node in self.model.nodes():
            if node.op_type == "Identity":
                if node.output[0] not in self.model.get_graphs_output_names():
                    self.model.replace_input_of_all_nodes(node.output[0], node.input[0])
                    nodes_to_remove.append(node)

        self.model.remove_nodes(nodes_to_remove)
        logger.info(f"Removed {len(nodes_to_remove)} Identity nodes")

    def remove_useless_cast_nodes(self):
        """Remove cast nodes that are not needed: input and output has same data type."""
        shape_infer = self.model.infer_runtime_shape(update=True)
        if shape_infer is None:
            return

        nodes_to_remove = []
        for node in self.model.nodes():
            if node.op_type == "Cast":
                input_dtype = self.get_dtype(shape_infer, node.input[0])
                output_dtype = self.get_dtype(shape_infer, node.output[0])
                if input_dtype and input_dtype == output_dtype:
                    nodes_to_remove.append(node)

        if nodes_to_remove:
            graph_input_names = set(self.model.get_graphs_input_names())
            graph_output_names = set(self.model.get_graphs_output_names())
            for node in nodes_to_remove:
                if bool(set(node.output) & graph_output_names):
                    if not bool(set(node.input) & graph_input_names):
                        self.model.replace_output_of_all_nodes(node.input[0], node.output[0])
                    else:
                        continue
                else:
                    self.model.replace_input_of_all_nodes(node.output[0], node.input[0])
                self.model.remove_node(node)
        logger.info(f"Removed {len(nodes_to_remove)} Cast nodes with output type same as input")

    def remove_useless_reshape_nodes(self):
        """Remove reshape node that is not needed based on symbolic shape inference: input and output has same shape"""
        shape_infer = self.model.infer_runtime_shape(update=True)
        if shape_infer is None:
            return

        nodes_to_remove = []
        for node in self.model.nodes():
            if node.op_type == "Reshape":
                input_shape = shape_infer.get_edge_shape(node.input[0])
                output_shape = shape_infer.get_edge_shape(node.output[0])
                if input_shape and output_shape and input_shape == output_shape:
                    logger.info(
                        f"Remove reshape node {node.name} since its input shape is same as output: {input_shape}"
                    )
                    nodes_to_remove.append(node)

        if nodes_to_remove:
            graph_input_names = set(self.model.get_graphs_input_names())
            graph_output_names = set(self.model.get_graphs_output_names())
            for node in nodes_to_remove:
                if bool(set(node.output) & graph_output_names):
                    if not bool(set(node.input) & graph_input_names):
                        self.model.replace_output_of_all_nodes(node.input[0], node.output[0])
                    else:
                        continue
                else:
                    self.model.replace_input_of_all_nodes(node.output[0], node.input[0])
                self.model.remove_node(node)


class NumpyHelper:
    @staticmethod
    def to_array(tensor: TensorProto, fill_zeros: bool = False) -> ndarray:
        # When weights are in external data format but not presented, we can still test the optimizer with two changes:
        # (1) set fill_zeros = True  (2) change load_external_data=False in optimizer.py
        if fill_zeros:
            from onnx import mapping

            return ndarray(
                shape=tensor.dims,
                dtype=mapping.TENSOR_TYPE_TO_NP_TYPE[tensor.data_type],
            )

        return numpy_helper.to_array(tensor)
