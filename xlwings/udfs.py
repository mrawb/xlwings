import os
import re
import os.path
import tempfile


from . import conversion


def xlfunc(f=None, **kwargs):
    def inner(f):
        if not hasattr(f, "__xlfunc__"):
            xlf = f.__xlfunc__ = {}
            xlf["name"] = f.__name__
            xlf["sub"] = False
            xlargs = xlf["args"] = []
            xlargmap = xlf["argmap"] = {}
            nArgs = f.__code__.co_argcount
            if f.__code__.co_flags & 4:  # function has an '*args' argument
                nArgs += 1
            for vpos, vname in enumerate(f.__code__.co_varnames[:nArgs]):
                xlargs.append({
                    "name": vname,
                    "pos": vpos,
                    "marshal": "var",
                    "vba": None,
                    "range": False,
                    "dtype": None,
                    "ndim": None,
                    "doc": "Positional argument " + str(vpos+1),
                    "vararg": True if vpos == f.__code__.co_argcount else False
                })
                xlargmap[vname] = xlargs[-1]
            xlf["ret"] = {
                "marshal": "var",
                "lax": True,
                "doc": f.__doc__ if f.__doc__ is not None else "Python function '" + f.__name__ + "' defined in '" + str(f.__code__.co_filename) + "'."
            }
        return f
    if f is None:
        return inner
    else:
        return inner(f)


def xlsub(f=None, **kwargs):
    def inner(f):
        f = xlfunc(**kwargs)(f)
        f.__xlfunc__["sub"] = True
        return f
    if f is None:
        return inner
    else:
        return inner(f)


def xlret(**kwargs):
    def inner(f):
        xlf = xlfunc(f).__xlfunc__
        xlr = xlf["ret"]
        xlr.update(kwargs)
        return f
    return inner


def xlarg(arg, **kwargs):
    def inner(f):
        xlf = xlfunc(f).__xlfunc__
        if arg not in xlf["argmap"]:
            raise Exception("Invalid argument name '" + arg + "'.")
        xla = xlf["argmap"][arg]
        xla.update(kwargs)
        return f
    return inner


udf_scripts = {}
def udf_script(filename):
    filename = filename.lower()
    mtime = os.path.getmtime(filename)
    if filename in udf_scripts:
        mtime2, vars = udf_scripts[filename]
        if mtime == mtime2:
            return vars
    vars = {}
    with open(filename, "r") as f:
        exec(compile(f.read(), filename, "exec"), vars)
    udf_scripts[filename] = (mtime, vars)
    return vars


def call_udf(script_name, func_name, args):
    script = udf_script(script_name)
    func = script[func_name]

    func_info = func.__xlfunc__
    args_info = func_info['args']
    ret_info = func_info['ret']

    args = list(args)
    for i, arg in enumerate(args):
        arg_info = args_info[i]
        args[i] = conversion.DefaultAccessor.read_value(arg, arg_info)

    ret = func(*args)

    return conversion.DefaultAccessor.write_value(ret, ret_info)


def import_udfs(script_path, xl_workbook):

    tab = '\t'

    tf = tempfile.NamedTemporaryFile(mode='w', delete=False)
    f = tf.file

    f.write('Attribute VB_Name = "xlwings_udfs"\n')

    script_vars = udf_script(script_path)

    for svar in script_vars.values():
        if hasattr(svar, '__xlfunc__'):
            xlfunc = svar.__xlfunc__
            xlret = xlfunc['ret']
            fname = xlfunc['name']

            ftype = 'Sub' if xlfunc['sub'] else 'Function'

            f.write(ftype + " " + fname + "(")

            first = True
            vararg = ''
            n_args = len(xlfunc['args'])
            for arg in xlfunc['args']:
                if not arg['vba']:
                    argname = arg['name']
                    if not first:
                        f.write(', ')
                    if arg['vararg']:
                        f.write('ParamArray ')
                        vararg = argname
                    f.write(argname)
                    if arg['vararg']:
                        f.write('()')
                    first = False
            f.write(')\n')
            if ftype == 'Function':
                f.write(tab + "If TypeOf Application.Caller Is Range Then On Error GoTo failed\n")

            if vararg != '':
                f.write(tab + "ReDim argsArray(1 to UBound(" + vararg + ") - LBound(" + vararg + ") + " + str(n_args) + ")\n")

            j = 1
            for arg in xlfunc['args']:
                if not arg['vba']:
                    argname = arg['name']
                    if arg['vararg']:
                        f.write(tab + "For k = LBound(" + vararg + ") To UBound(" + vararg + ")\n")
                        argname = vararg + "(k)"
                    if not arg['range']:
                        f.write(tab + "If TypeOf " + argname + " Is Range Then " + argname + " = " + argname + ".Value\n")
                    if arg['vararg']:
                        f.write(tab + "argsArray(" + str(j) + " + k - LBound(" + vararg + ")) = " + argname + "\n")
                        f.write(tab + "Next k\n")
                    else:
                        if vararg != "":
                            f.write(tab + "argsArray(" + str(j) + ") = " + argname + "\n")
                            j += 1

            if vararg != '':
                args_vba = 'argsArray'
            else:
                args_vba = 'Array(' + ', '.join(arg['vba'] or arg['name'] for arg in xlfunc['args']) + ')'

            if ftype == "Sub":
                f.write('\tPy.CallMacro PyScriptPath, "{fname}", {args_vba}, ThisWorkbook\n'.format(
                    fname=fname,
                    args_vba=args_vba,
                ))
            else:
                f.write('\tSet {fname} = Py.CallUDF(PyScriptPath, "{fname}", {args_vba})\n'.format(
                    fname=fname,
                    args_vba=args_vba,
                ))
                marshal = xlret["marshal"]
                if marshal == "auto":
                    f.write(tab + "If TypeOf Application.Caller Is Range Then " + fname + " = Py.Var(" + fname + ", " + str(xlret["lax"]) + ")\n")
                elif marshal == "var":
                    f.write(tab + fname + " = Py.Var(" + fname + ", " + str(xlret["lax"]) + ")\n")
                elif marshal == "str":
                    f.write(tab + fname + " = Py.Str(" + fname + ")\n")

            if ftype == "Function":
                f.write(tab + "Exit " + ftype + "\n")
                f.write("failed:\n")
                f.write(tab + fname + " = Err.Description\n")
            f.write("End " + ftype + "\n")
            f.write("\n")

    tf.close()

    try:
        xl_workbook.VBProject.VBComponents.Remove(xl_workbook.VBProject.VBComponents("xlwings_udfs"))
    except:
        pass
    xl_workbook.VBProject.VBComponents.Import(tf.name)

    for svar in script_vars.values():
        if hasattr(svar, '__xlfunc__'):
            xlfunc = svar.__xlfunc__
            xlret = xlfunc['ret']
            xlargs = xlfunc['args']
            fname = xlfunc['name']
            fdoc = xlret['doc'][:255]
            n_args = 0
            for arg in xlargs:
                if not arg['vba']:
                    n_args += 1

            excel_version = [int(x) for x in re.split("[,\\.]", xl_workbook.Application.Version)]
            if n_args > 0 and excel_version[0] >= 14:
                argdocs = []
                for arg in xlargs:
                    if not arg['vba']:
                        argdocs.append(arg['doc'][:255])
                xl_workbook.Application.MacroOptions("'" + xl_workbook.Name + "'!" + fname, Description=fdoc, ArgumentDescriptions=argdocs)
            else:
                xl_workbook.Application.MacroOptions("'" + xl_workbook.Name + "'!" + fname, Description=fdoc)

    # try to delete the temp file - doesn't matter too much if it fails
    try:
        os.unlink(tf.name)
    except:
        pass