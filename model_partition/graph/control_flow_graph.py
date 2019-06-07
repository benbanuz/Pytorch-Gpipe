
import torch.nn as nn
import torch
from enum import Enum
import re
from copy import copy



def build_graph_from_trace(model, *sample_batch, max_depth=100, weights=None, basic_block=None, device="cuda"):
    device = "cpu" if not torch.cuda.is_available() else device
    model_class_name = type(model).__name__

    buffer_param_names = _buffer_and_params_scopes(model,  model_class_name)

    weights = weights if weights != None else {}

    layerNames = _profiled_layers(
        model, max_depth, prefix=model_class_name, basic_block=basic_block)

    # trace the model and build a graph
    inputs = tuple(map(lambda t: t.to(device), sample_batch))
    model.to(device)
    num_inputs = len(inputs)

    with torch.no_grad():
        trace_graph, _ = torch.jit.get_trace_graph(model, inputs)
        trace_graph = trace_graph.graph()

    return Graph(layerNames, num_inputs, buffer_param_names, trace_graph, weights)


class NodeTypes(Enum):
    IN = 1
    BUFF_PARAM = 2
    LAYER = 3
    OP = 4

    def __repr__(self):
        return self.name


class Node():
    '''
    a simple graph node for directed graphs

    Fields:
    ------
    scope:
     the operation/layer the node represents
    idx:
     a serial number of the node for convience
    node_type:
     an enum representing if the node is an input Layer or operator(like arithmetic ops)
    incoming_nodes:
     the nodes who have edges from them to this node
    out_nodes:
     the nodes who have edges from this node

     parallel edges in the same direction are not allowed
    '''

    def __init__(self, scope, idx, node_type: NodeTypes,output_shape, incoming_nodes=None, weight=0, part=10):
        self.scope = scope
        self.idx = idx
        self.type = node_type
        self.out_nodes = set()
        self.weight = weight
        self.part = part
        self.output_shape=[output_shape] if isinstance(output_shape,tuple) else output_shape
        self.in_nodes = incoming_nodes if isinstance(incoming_nodes, set) else set()

    def add_out_node(self, node):
        if isinstance(node, Node):
            self.out_nodes.add(node)
        if isinstance(node, set):
            self.out_nodes.update(node)

    def add_in_node(self, node):
        if isinstance(node, Node):
            self.in_nodes.add(node)
        if isinstance(node, set):
            self.in_nodes.update(node)

    def remove_in_node(self, node):
        if isinstance(node, Node):
            self.in_nodes.discard(node)
        if isinstance(node, set):
            self.in_nodes.difference_update(node)

    def remove_out_node(self, node):
        if isinstance(node, Node):
            self.out_nodes.discard(node)
        if isinstance(node, set):
            self.out_nodes.difference_update(node)

    def __repr__(self):
        out_idx = {node.idx for node in self.out_nodes}
        in_idx = {node.idx for node in self.in_nodes}
        return f"node {self.idx} in scope {self.scope} of type {self.type} flows to {out_idx} gathers {in_idx}\n"


class Graph():
    '''
    a graph representing the control flow of a model
    built from a pytorch trace graph.
    the graph vertices are specified using the given profiled layer names\n
    and will also include basic pytorch ops that connect them.
    the edges represent the data flow.
    '''

    def __init__(self, profiled_layers, num_inputs, buffer_param_names, trace_graph, weights: dict):
        self.nodes = []
        self.profiled_layers = profiled_layers
        self.num_inputs_buffs_params = 0
        self.num_inputs = num_inputs
        self.buffer_param_names = buffer_param_names
        self._build_graph(trace_graph)

        for node in self.nodes:
            node.weight = weights.get(node.scope, node.weight)

    def _build_graph(self, trace_graph):
        self._add_IO_nodes(trace_graph.inputs())
        self._add_OP_nodes(trace_graph.nodes())
        self._remove_constant_nodes()
        self._remove_nodes_that_go_nowhere(trace_graph.outputs())
        self._normalize_indices()

    def _add_IO_nodes(self, input_nodes):
        '''
        add nodes representing the input and params/buffs of the model
        '''
        for idx, node in enumerate(input_nodes):
            node_weight = 1
            # input/buff/parm weight is it's size
            for d in node.type().sizes():
                node_weight *= d

            if idx < self.num_inputs:
                node_type = NodeTypes.IN
                node_scope = f"input{idx}"
            else:
                node_type = NodeTypes.BUFF_PARAM
                node_scope = self.buffer_param_names[idx - self.num_inputs]

            output_shape = tuple(node.type().sizes())
            new_node = Node(node_scope, idx,node_type,output_shape, weight=node_weight)
            self.nodes.append(new_node)

            self.num_inputs_buffs_params += 1

    def _add_OP_nodes(self, OP_nodes):
        '''
        add nodes representing the layers/ops of the model
        '''
        num_extra_nodes = 0
        for idx, trace_node in enumerate(OP_nodes):
            node_scope = self._find_encasing_layer(trace_node.scopeName())
            input_nodes = {self.nodes[i.unique()]
                           for i in trace_node.inputs()}
            node_idx = self.num_inputs_buffs_params+idx+num_extra_nodes
            
            #node output_shape
            m = re.match(r".*Float\(([\d\s\,]+)\).*",
                         str(next(trace_node.outputs())))
            if m:
                shape = m.group(1)
                shape = shape.split(",")
                shape = tuple(map(int, shape))
            else:
                shape = (1,)

            new_node = None

            # profiled Layer
            if node_scope != "":
                new_node = Node(node_scope, node_idx,
                                NodeTypes.LAYER,shape, input_nodes)
            # unprofiled OP
            else:
                node_scope = trace_node.scopeName() + \
                    "/"+trace_node.kind() + str(idx)
                new_node = Node(node_scope, node_idx,
                                NodeTypes.OP,shape, input_nodes)
    
            # add incoming edges
            for node in input_nodes:
                node.add_out_node(new_node)

            self.nodes.append(new_node)

            # add node for each output
            for i, _ in enumerate(trace_node.outputs()):
                if i != 0:
                    out_node: Node = copy(new_node)
                    #TODO check if correct
                    #it appears those are dummpy outputs so we just add them for bookkeeping and remove them later
                    out_node.out_nodes=set()

                    out_node.idx += i
                    out_node.add_in_node(self.nodes[node_idx+i-1])
                    self.nodes[node_idx+i-1].add_out_node(out_node)
                    self.nodes.append(out_node)
                    num_extra_nodes += 1

    def _remove_constant_nodes(self):
        # remove nodes representing constants as they do not provide any useful info
        self._remove_nodes(lambda n: "::Constant" in n.scope)

    def _find_encasing_layer(self, scopeName: str):
        '''
        find the closest scope which encases scopeName
        '''
        # unfortunately the trace graph shows only basic layers and ops
        # so we need to manually find a profiled layer that encases the op
        most_specific_scope = ""
        for layer_scope in self.profiled_layers:
            if scopeName.startswith(layer_scope) and len(layer_scope) > len(most_specific_scope):
                most_specific_scope = layer_scope

        return most_specific_scope

    def _remove_nodes_that_go_nowhere(self,trace_outputs):
        out_list=list(trace_outputs)
        out_indices=list(map(lambda n: int(n.uniqueName()),out_list))
        def going_nowhere(node):
            return (not node.out_nodes) and (not node.idx in out_indices)
            
        self._remove_nodes(going_nowhere)

    def _remove_nodes(self, condition,reverse=False):
        changed=True
        while changed:
            changed=False
            optimized_graph = []

            nodes = reversed(self.nodes) if reverse else self.nodes

            for node in nodes:
                if condition(node):
                    changed=True
                    # connect inputs to outputs directly
                    for in_node in node.in_nodes:
                        in_node.remove_out_node(node)
                        in_node.add_out_node(node.out_nodes)
                    for out_node in node.out_nodes:
                        out_node.remove_in_node(node)
                        out_node.add_in_node(node.in_nodes)
                else:
                    optimized_graph.append(node)

            self.nodes = optimized_graph

    def __getitem__(self, key):
        return self.nodes[key]

    def __repr__(self):
        discription = ''
        for node in self.nodes:
            discription = f"{discription}\n{node}"
        return discription

    def get_nodes(self):
        return self.nodes

    def get_weights(self):
        return [node.weight for node in self.nodes]

    def adjacency_list(self,directed=False):
        if not directed:
            return [[n.idx for n in node.out_nodes.union(node.in_nodes)] for node in self.nodes]
        return [[n.idx for n in node.out_nodes] for node in self.nodes]

    def _normalize_indices(self):
        for idx, node in enumerate(self.nodes):
            node.idx = idx

    def build_dot(self, show_buffs_params=False,show_weights=True):
        '''
        return a graphviz representation of the graph
        '''

        theme = {"background_color": "#FFFFFF",
                 "fill_color": "#E8E8E8",
                 "outline_color": "#000000",
                 "font_color": "#000000",
                 "font_name": "Times",
                 "font_size": "10",
                 "margin": "0,0",
                 "padding":  "1.0,0.5"}
        from graphviz import Digraph

        dot = Digraph()
        dot.attr("graph",
                 concentrate="true",
                 bgcolor=theme["background_color"],
                 color=theme["outline_color"],
                 fontsize=theme["font_size"],
                 fontcolor=theme["font_color"],
                 fontname=theme["font_name"],
                 margin=theme["margin"],
                 rankdir="TB",
                 pad=theme["padding"])

        dot.attr("node", shape="box",
                 style="filled", margin="0,0",
                 fillcolor=theme["fill_color"],
                 color=theme["outline_color"],
                 fontsize=theme["font_size"],
                 fontcolor=theme["font_color"],
                 fontname=theme["font_name"])

        dot.attr("edge", style="solid",
                 color=theme["outline_color"],
                 fontsize=theme["font_size"],
                 fontcolor=theme["font_color"],
                 fontname=theme["font_name"])

        colors={0:'grey',1:'green',2:'red',3:'yellow',4:'orange',5:'brown',6:'purple',7:'pink',10:"white"}


        def hide_node(node):
            return (node.type == NodeTypes.BUFF_PARAM) and (not show_buffs_params)

        for node in self.nodes:
            if hide_node(node):
                continue
            label = node.scope
            if show_weights and node.weight != 0:
                label = f"{label}\n {node.weight}"
            label=f"{label}\n output_shape {node.output_shape}"
            dot.node(str(node.idx), label, fillcolor=colors[node.part])

        for node in self.nodes:
            if hide_node(node):
                continue
            for in_node in node.in_nodes:
                if hide_node(in_node):
                    continue
                dot.edge(str(in_node.idx), str(node.idx))

        return dot

    def display(self, show_buffs_params=False,show_weights=True):
        try:
            from IPython.core.display import display_svg
            display_svg(self.build_dot(show_buffs_params,show_weights=show_weights), raw=False)
        except ImportError as _:
            print("only works in python notebooks")

    def save(self,file_name,directory, show_buffs_params=False,show_weights=True):
        dot = self.build_dot(show_buffs_params,show_weights=show_weights)
        dot.format = "pdf"
        import os
        if os.path.exists(f"{directory}/{file_name}.pdf"):
            os.remove(f"{directory}/{file_name}.pdf")
        dot.render(file_name, directory=directory, cleanup=True)

# scope names of all profiled layers in the model
def _profiled_layers(module: nn.Module, depth, prefix, basic_block):
    names = []
    for name, sub_module in module._modules.items():
        if len(list(sub_module.children())) == 0 or (basic_block != None and isinstance(sub_module, basic_block)) or depth == 0:
            names.append(
                prefix+"/"+type(sub_module).__name__+f"[{name}]")
        else:
            names = names + _profiled_layers(sub_module, depth-1, prefix +
                                             "/"+type(sub_module).__name__+f"[{name}]",basic_block)
    return names

# scope names of all params and buffs in the model
# we discover them manually because the tracer does not provide this info
def _buffer_and_params_scopes(module: nn.Module,prefix):
    names = []
    # params
    for item_name, item in module.named_parameters(recurse=False):
        names.append(f"{prefix}/{type(item).__name__}[{item_name}]")

    # buffs
    for item_name, item in module.named_buffers(recurse=False):
        names.append(f"{prefix}/{type(item).__name__}[{item_name}]")

    # recurse
    for name, sub_module in module._modules.items():
        names = names + _buffer_and_params_scopes(sub_module, prefix +
                                             "/"+type(sub_module).__name__+f"[{name}]")

    return names