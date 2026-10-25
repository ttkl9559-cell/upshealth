"""
极简「未定义全局名」检查器（替代 pyflakes，零依赖）。

原理：用 symtable 找出所有被当作全局引用的名字，再与模块级真正定义过的名字、
内置名比对。剩下的就是运行时可能抛 NameError 的可疑点 —— 这类错误
编译期发现不了，只有在特定分支（比如 UPS 状态恰好发生变化）执行时才会炸。
"""
import builtins
import os
import symtable
import sys


def collect(table, defined, suspects, path=""):
    for sym in table.get_symbols():
        name = sym.get_name()
        if sym.is_assigned() or sym.is_imported() or sym.is_namespace():
            if table.get_type() == "module":
                defined.add(name)
        elif table.get_type() == "module":
            # 模块级只读引用：可能是后面才定义的全局，交给最终比对
            pass
        if sym.is_global() and not (sym.is_assigned() or sym.is_imported() or sym.is_namespace()):
            suspects.add((name, path or "<module>"))
    for child in table.get_children():
        collect(child, defined, suspects, path + "/" + child.get_name() if path else child.get_name())


def main(paths):
    rc = 0
    for p in paths:
        src = open(p, encoding="utf-8").read()
        st = symtable.symtable(src, os.path.basename(p), "exec")
        defined, suspects = set(), set()
        collect(st, defined, suspects)
        known = defined | set(dir(builtins)) | {
            "__file__", "__name__", "__doc__", "__spec__",
            "__loader__", "__package__", "__builtins__",
        }
        bad = sorted({(n, where) for n, where in suspects if n not in known})
        print("== %s  定义 %d 个全局名，可疑 %d 处" % (p, len(defined), len(bad)))
        for n, where in bad:
            print("   UNDEFINED  %-24s  used in %s" % (n, where))
            rc = 1
    return rc


if __name__ == "__main__":
    args = sys.argv[1:] or ["app/server.py"]
    sys.exit(main(args))
