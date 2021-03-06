# Copyright 2019-2020 ETH Zurich and the DaCe authors. All rights reserved.
""" This module contains classes that implement a map->for loop transformation.
"""

import dace
from dace import data, registry, symbolic
from dace.sdfg import SDFG, SDFGState
from dace.sdfg import nodes
from dace.sdfg import utils as sdutil
from dace.transformation import transformation
from dace.transformation.helpers import nest_state_subgraph
from typing import Tuple


@registry.autoregister_params(singlestate=True)
class MapToForLoop(transformation.Transformation):
    """ Implements the Map to for-loop transformation.

        Takes a map and enforces a sequential schedule by transforming it into
        a state-machine of a for-loop. Creates a nested SDFG, if necessary.
    """

    _map_entry = nodes.MapEntry(nodes.Map("", [], []))

    @staticmethod
    def annotates_memlets():
        return True

    @staticmethod
    def expressions():
        return [sdutil.node_path_graph(MapToForLoop._map_entry)]

    @staticmethod
    def can_be_applied(graph, candidate, expr_index, sdfg, strict=False):
        # Only uni-dimensional maps are accepted.
        map_entry = graph.nodes()[candidate[MapToForLoop._map_entry]]
        if len(map_entry.map.params) > 1:
            return False

        return True

    @staticmethod
    def match_to_str(graph, candidate):
        map_entry = graph.nodes()[candidate[MapToForLoop._map_entry]]
        return map_entry.map.label + ': ' + str(map_entry.map.params)

    def apply(self, sdfg) -> Tuple[nodes.NestedSDFG, SDFGState]:
        """ Applies the transformation and returns a tuple with the new nested
            SDFG node and the main state in the for-loop. """

        # Retrieve map entry and exit nodes.
        graph = sdfg.nodes()[self.state_id]
        map_entry = graph.nodes()[self.subgraph[MapToForLoop._map_entry]]
        map_exit = graph.exit_node(map_entry)

        loop_idx = map_entry.map.params[0]
        loop_from, loop_to, loop_step = map_entry.map.range[0]

        # Turn the map scope into a nested SDFG
        node = nest_state_subgraph(sdfg, graph, graph.scope_subgraph(map_entry))

        nsdfg: SDFG = node.sdfg
        nstate: SDFGState = nsdfg.nodes()[0]

        # If map range is dynamic, replace loop expressions with memlets
        param_to_edge = {}
        for edge in nstate.in_edges(map_entry):
            if edge.dst_conn and not edge.dst_conn.startswith('IN_'):
                param = '__DACE_P%d' % len(param_to_edge)
                repldict = {symbolic.pystr_to_symbolic(edge.dst_conn): param}
                param_to_edge[param] = edge
                loop_from = loop_from.subs(repldict)
                loop_to = loop_to.subs(repldict)
                loop_step = loop_step.subs(repldict)

        # Avoiding import loop
        from dace.codegen.targets.cpp import cpp_array_expr

        def replace_param(param):
            param = symbolic.symstr(param)
            for p, pval in param_to_edge.items():
                # TODO: Correct w.r.t. connector type
                param = param.replace(p, cpp_array_expr(nsdfg, pval.data))
            return param

        # End of dynamic input range

        # Create a loop inside the nested SDFG
        nsdfg.add_loop(None, nstate, None, loop_idx, replace_param(loop_from),
                       '%s < %s' % (loop_idx, replace_param(loop_to + 1)),
                       '%s + %s' % (loop_idx, replace_param(loop_step)))

        # Skip map in input edges
        for edge in nstate.out_edges(map_entry):
            src_node = nstate.memlet_path(edge)[0].src
            nstate.add_edge(src_node, None, edge.dst, edge.dst_conn, edge.data)
            nstate.remove_edge(edge)

        # Skip map in output edges
        for edge in nstate.in_edges(map_exit):
            dst_node = nstate.memlet_path(edge)[-1].dst
            nstate.add_edge(edge.src, edge.src_conn, dst_node, None, edge.data)
            nstate.remove_edge(edge)

        # Remove nodes from dynamic map range
        nstate.remove_nodes_from(
            [e.src for e in dace.sdfg.dynamic_map_inputs(nstate, map_entry)])
        # Remove scope nodes
        nstate.remove_nodes_from([map_entry, map_exit])

        return node, nstate
