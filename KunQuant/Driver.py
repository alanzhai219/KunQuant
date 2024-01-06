from KunQuant.passes import *
from KunQuant.Stage import Function
from KunQuant.Op import Input, Output, OpBase, Rank
from typing import Dict, List
import typing
from collections import OrderedDict
from dataclasses import dataclass
from KunQuant.passes import Util as PassUtil

def optimize(f: Function)->None:
    if PassUtil.debug_mode:
        print("Before optimize: ", f)
    decompose(f)
    expr_fold(f)
    temp_window_elim(f)
    special_optimize(f)

def post_optimize(impl: List[Function])->None:
    if PassUtil.debug_mode:
        print("Post optimize:","=====================")
    for f in impl:
        temp_window_elim(f)

@dataclass
class _Buffer:
    idx: int
    name: str
    kind: str

    def __str__(self) -> str:
        return f'{{{self.idx}, "{self.name}", BufferKind::{self.kind}}}'

@dataclass
class _Partition:
    name: str
    idx: int
    in_buf: List[_Buffer]
    out_buf: List[_Buffer]
    outs: List['_Partition'] = None
    num_in_dep = 0
    is_rank = False

def compileit(f: Function, module_name: str, input_stride: int, output_stride: int, partition_factor = 4):
    input_name_to_idx: Dict[str, int] = dict()
    buffer_names: List[_Buffer] = []
    partitions: typing.OrderedDict[str, _Partition] = OrderedDict()
    def insert_name(op: OpBase, kind: str) -> _Buffer:
        nonlocal input_name_to_idx
        name = op.attrs["name"]
        if name not in input_name_to_idx:
            newidx = len(input_name_to_idx)
            newbuf =  _Buffer(newidx, name, kind)
            input_name_to_idx[name] = newidx
            buffer_names.append(newbuf)
            return newbuf
        return buffer_names[input_name_to_idx[name]]

    for op in f.ops:
        if isinstance(op, Input):
            insert_name(op, "INPUT")
        elif isinstance(op, Output):
            insert_name(op, "OUTPUT")

    optimize(f)
    mainf, impl = do_partition(f, partition_factor)
    post_optimize(impl)

    impl_src = ['''#include <Kun/Context.hpp>
#include <Kun/Module.hpp>
#include <Kun/Ops.hpp>

using namespace kun;
using namespace kun::ops;
''']    
    for func in impl:
        pins = []
        pouts = []
        ins = []
        outs = []
        for op in func.ops:
            if isinstance(op, Input):
                buf = insert_name(op, "TEMP")
                pins.append(buf)
                ins.append(op)
            elif isinstance(op, Output):
                buf = insert_name(op, "TEMP")
                pouts.append(buf)
                outs.append(op)
        src = codegen_cpp(func, input_stride, output_stride, input_name_to_idx, ins, outs)
        impl_src.append(src)
        newparti = _Partition(func.name, len(partitions), pins, pouts)
        if len(func.ops) == 3 and isinstance(func.ops[1], Rank):
            newparti.is_rank = True
        partitions[func.name] = newparti
    for p in mainf.ops:
        cur = partitions[p.attrs["name"]]
        cur.num_in_dep = len(p.inputs)
        cur.outs = [partitions[use.attrs["name"]] for use in mainf.op_to_id[p].uses]

    buffer_src = ",\n".join(["    "+ str(v) for v in buffer_names])
    impl_src.append(f"static BufferInfo __buffers[]{{\n{buffer_src}\n}};")

    parti_buffer_src = []
    for name, parti in partitions.items():
        buffer_lines = ", ".join([f"&__buffers[{v.idx}]" for v in parti.in_buf])
        parti_buffer_src.append(f"static BufferInfo *stage_{name}_in_buf[] = {{{buffer_lines}}};")
        buffer_lines = ", ".join([f"&__buffers[{v.idx}]" for v in parti.out_buf])
        parti_buffer_src.append(f"static BufferInfo *stage_{name}_out_buf[] = {{{buffer_lines}}};")
    impl_src.append("\n".join(parti_buffer_src))

    parti_dep_src = "\n".join([f"extern Stage *stage_{name}_dep[{len(parti.outs)}];" if len(parti.outs) else f"Stage **stage_{name}_dep = nullptr;"
                                for name, parti in partitions.items()])
    impl_src.append(f'''namespace {{
{parti_dep_src}
}}
''')
    
    parti_info_src = ",\n".join([f'''    {{/*f*/ stage_{parti.name}, /*dependers*/ stage_{parti.name}_dep, /*num_dependers*/ {len(parti.outs)},
     /*in_buffers*/ stage_{parti.name}_in_buf, /*num_in_buffers*/ {len(parti.in_buf)},
     /*out_buffers*/ stage_{parti.name}_out_buf, /*num_out_buffers*/ {len(parti.out_buf)}, /*pending_out*/ {parti.num_in_dep},
     /*num_tasks*/ TaskExecKind::{"SLICE_BY_TIME" if parti.is_rank else "SLICE_BY_STOCK"}, /*id*/ {parti.idx}}}''' for parti in partitions.values()])
    impl_src.append(f'''static Stage __stages[] = {{
{parti_info_src}
}};''')
                    
    parti_dep_src = []
    for name, parti in partitions.items():
        if len(parti.outs):
            details = ", ".join([f"&__stages[{out.idx}]" for out in parti.outs])
            parti_dep_src.append(f"Stage *stage_{parti.name}_dep[] = {{{details}}};")
    parti_dep_src2 = "\n".join(parti_dep_src)
    impl_src.append(f'''namespace {{
{parti_dep_src2}
}}
''')
    impl_src.append(f'''KUN_API Module {module_name}{{
    {len(partitions)},
    __stages,
    {len(buffer_names)},
    __buffers
}};''')
    return "\n\n".join(impl_src)
