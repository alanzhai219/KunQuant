from KunQuant.Driver import KunCompilerConfig
from KunTestUtil import ref_alpha101, gen_data
import numpy as np
import pandas as pd
import sys
import time
import os
from KunQuant.jit import cfake
from KunQuant.Op import Builder, Input, Output
from KunQuant.Stage import Function
from KunQuant.predefined.Alpha101 import AllData, all_alpha
from KunQuant.runner import KunRunner as kr

def check_alpha101(avx: str):
    builder = Builder()
    with builder:
        all_data = AllData(low=Input("low"),high=Input("high"),close=Input("close"),open=Input("open"), amount=Input("amount"), volume=Input("volume"))
        for f in all_alpha:
            out = f(all_data)
            Output(out, f.__name__)
    simd_len = 16 if avx == "avx512" else 8
    f = Function(builder.ops)
    return "alpha_101", f, KunCompilerConfig(blocking_len=simd_len, output_layout="TS", options={"opt_reduce": True, "fast_log": True})

def check_alpha101_double(avx: str):
    builder = Builder()
    with builder:
        all_data = AllData(low=Input("low"),high=Input("high"),close=Input("close"),open=Input("open"), amount=Input("amount"), volume=Input("volume"))
        for f in all_alpha:
            # if f.__name__ != "alpha043" and f.__name__ != "alpha039":
            #     continue
            out = f(all_data)
            Output(out, f.__name__)
    simd_len = 8 if avx == "avx512" else 4
    f = Function(builder.ops)
    return "alpha_101_double", f, KunCompilerConfig(blocking_len=simd_len, input_layout="TS", output_layout="TS", dtype="double", options={"opt_reduce": True, "fast_log": True})

def check_alpha101_stream(avx):
    builder = Builder()
    cnt = 0
    with builder:
        all_data = AllData(low=Input("low"),high=Input("high"),close=Input("close"),open=Input("open"), amount=Input("amount"), volume=Input("volume"))
        for f in all_alpha:
            out = f(all_data)
            Output(out, f.__name__)
            cnt += 1
    simd_len = 16 if avx == "avx512" else 8
    f = Function(builder.ops)
    return "alpha_101_stream", f, KunCompilerConfig(blocking_len=simd_len, partition_factor=8, output_layout="STREAM", options={"opt_reduce": False, "fast_log": True})
 
def count_unmatched_elements(arr1: np.ndarray, arr2: np.ndarray, atol=1e-8, rtol=1e-5, equal_nan=False):
    # Check if arrays have the same shape
    if arr1.shape != arr2.shape:
        raise ValueError("Input arrays must have the same shape")

    # Mask for NaN equality
    nan_equal_mask = np.isnan(arr1) & np.isnan(arr2)

    # Check absolute and relative differences
    absolute_diff = np.abs(arr1 - arr2)
    tol = np.maximum(np.abs(arr1), np.abs(arr2))* rtol + atol

    # Mask for elements that meet the allclose criteria
    close_mask = (absolute_diff <= tol) | (nan_equal_mask if equal_nan else False)

    # Count unmatched elements
    unmatched_count = np.sum(~close_mask)

    return unmatched_count, close_mask

def rand_float(stocks, low = 0.9, high = 1.11):
    return np.random.uniform(low, high, size= stocks)



def gen_stock_data(low, high, stocks, num_time, stddev):
    xopen = np.random.uniform(low, high, size = stocks).astype("float32")
    xvol = np.random.uniform(5, 10, size = stocks).astype("float32")
    outopen = np.empty((stocks, num_time), dtype="float32")
    outclose = np.empty((stocks, num_time), dtype="float32")
    outhigh = np.empty((stocks, num_time), dtype="float32")
    outlow = np.empty((stocks, num_time), dtype="float32")
    outvol = np.empty((stocks, num_time), dtype="float32")
    outamount = np.empty((stocks, num_time), dtype="float32")
    for i in range(num_time):
        xopen *= np.random.normal(1, stddev, size = (stocks)).astype("float32")
        xclose = xopen * rand_float(stocks, 0.99, 1.01)
        xhigh = np.maximum.reduce([xopen * rand_float(stocks, 0.99, 1.03), xopen, xclose])
        xlow = np.minimum.reduce([xopen * rand_float(stocks, 0.97, 1.01), xopen, xclose, xhigh])
        xvol *= np.random.normal(1, stddev, size = (stocks)).astype("float32")
        xamount = xvol * xopen * rand_float(stocks)
        outopen[:,i] = xopen
        outclose[:,i] = xclose
        outhigh[:,i] = xhigh
        outlow[:,i] = xlow
        outvol[:,i] = xvol
        outamount[:,i] = xamount
    return outopen, outclose, outhigh, outlow, outvol, outamount



def make_ST_STs(blocking_num):
    def TS_STs(data: np.ndarray) -> np.ndarray:
        if data.shape[0] % blocking_num == 0:
            return np.ascontiguousarray(data.reshape((-1, blocking_num, data.shape[1])).transpose((0, 2, 1)))
        buf = np.zeros(((data.shape[0] // blocking_num + 1) * blocking_num, data.shape[1]), dtype=data.dtype)
        buf[:data.shape[0], :] = data
        return TS_STs(buf)
    return TS_STs


def STs_TS(data: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(data.transpose((0, 2, 1)).reshape((-1, data.shape[1])))

def TS_ST(data: np.ndarray) -> np.ndarray:
    return data.transpose()

def ST_TS(data: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(data.transpose())

def make_data_and_ref(num_stock, num_time, ischeck, input_ST8t, dtype="float32"):
    rng = np.random.get_state()
    start = time.time()
    dopen, dclose, dhigh, dlow, dvol, damount = gen_data.gen_stock_data2(0.5, 100, num_stock, num_time, 0.03 if num_time > 1000 else 0.05, dtype)
    end = time.time()
    print(f"DataGen takes: {end-start:.6f} seconds")
    transpose_func = make_ST_STs(input_ST8t) if input_ST8t else ST_TS
    my_input = {"high": transpose_func(dhigh), "low": transpose_func(dlow), "close": transpose_func(dclose), "open": transpose_func(dopen), "volume": transpose_func(dvol), "amount": transpose_func(damount)}
    ref = None
    if ischeck:
        df_dclose = pd.DataFrame(dclose.transpose())
        df_dopen = pd.DataFrame(dopen.transpose())
        df_vol = pd.DataFrame(dvol.transpose())
        df_low = pd.DataFrame(dlow.transpose())
        df_high = pd.DataFrame(dhigh.transpose())
        df_amount = pd.DataFrame(damount.transpose())
        start = time.time()
        ref = ref_alpha101.get_alpha({"S_DQ_HIGH": df_high, "S_DQ_LOW": df_low, "S_DQ_CLOSE": df_dclose, 'S_DQ_OPEN': df_dopen, "S_DQ_VOLUME": df_vol, "S_DQ_AMOUNT": df_amount})
        end = time.time()
        print(f"Ref takes: {end-start:.6f} seconds")
    return my_input, ref

tolerance = {
    "atol" : {
        # alpha013 has rank(cov(rank(X), rank(Y))). Output of cov seems to have very similar results
        # like 1e-6 and 0. Thus the rank result will be different
        "alpha003": 1e-4,
        "alpha013": 1e-4,
        "alpha013": 0.2,
        "alpha016": 0.2,
        "alpha015": 1,
        "alpha005": 0.2,
        "alpha002": 0.2,
        "alpha031": 0.1,
        "alpha034": 0.05,
        "alpha043": 1,
        "alpha044": 3e-5,
        # alpha045 has "correlation(ts_sum(self.close, 5), ts_sum(self.close, 20), 2)". It is 1/-1 in our synthetic data
        # because "close" is generated by normal distribution. We are using float32, so may +/-0.999X are created and ranks
        # are not stable 
        "alpha045": 0.3,
        "alpha050": 0.15,
        "alpha055": 0.1,
        "alpha071": 0.51,
        "alpha078": 0.1,
        "alpha085": 0.1,
        "alpha083": 0.05,
        "alpha088": 0.05,
        "alpha094": 0.05,
        "alpha096": 1,
        "alpha098": 0.1,
    },
    "rtol" : {
        "alpha013": 0.1,
        "alpha017": 0.1,
        "alpha018": 0.05,
        "alpha014": 1e-2,
        "alpha016": 0.1,
        "alpha027": 0.1,
        "alpha036": 2e-2,
        "alpha039": 0.1,
        "alpha043": 0.1,
        "alpha045": 0.1,
        "alpha050": 1e-1,
        "alpha072": 2e-1,
        "alpha077": 1e-1,
        "alpha078": 1e-1,
        "alpha083": 0.05,
        "alpha088": 1e-1,
        "alpha094": 1e-1,
        "alpha098": 1e-1,
    },
    "bad_count": {
        "alpha008": 0.001,
        "alpha022": 0.001,
        "alpha027": 0.0985,
        "alpha021": 0.001,
        "alpha045": 0.001,
        "alpha045": 0.07,
        "alpha050": 0.003,
        # hard selecting numbers >0
        "alpha053": 0.001,
        "alpha061": 0.002,
        "alpha062": 0.001,
        "alpha064": 0.001,
        "alpha065": 0.002,
        "alpha066": 0.002,
        # corr on rank, will produce NAN
        "alpha071": 0.04,
        "alpha072": 0.025,
        "alpha074": 0.04,
        "alpha075": 0.09,
        "alpha077": 0.001,
        "alpha078": 0.021,
        "alpha081": 0.27,
        "alpha008": 0.0005,
        "alpha084": 0.0009,
        "alpha085": 0.005,
        "alpha088": 0.005,
        "alpha092": 0.17,
        "alpha094": 0.01,
        "alpha096": 0.10,
        "alpha098": 0.08,
        "alpha099": 0.0005
    },
    # the tolerance count for non 2^N count of stocks
    "bad_count_unaligned": {
        "alpha013": 0.001,
        # log(X) will lose the ordering of values. x1=2.9499999999999997 x2=2.9500000000000001
        # and log(x1)==log(x2). alpha029 has rank(log(X))
        "alpha029": 0.04,
        "alpha034": 0.001,
        "alpha062": 0.005,
        "alpha074": 0.089,
        "alpha078": 0.065,
        "alpha083": 0.005,
        "alpha092": 0.275,
        "alpha096": 0.195,
        "alpha098": 0.95,
    },
    "skip_head": {"alpha096","alpha098"}
}
def is_power_of_two(n):
    return (n != 0) and (n & (n-1) == 0)

def check_result(out, ref, outnames, start_window, num_stock, start_time, num_time):
    rtol=0.01
    atol=1e-5
    done = True
    for k in outnames:
        cur_rtol = tolerance["rtol"].get(k, rtol)
        cur_atol = tolerance["atol"].get(k, atol)
        check_start = 0
        if start_time or k in tolerance["skip_head"]:
            check_start = start_window[k] + start_time
        v = out[k][:,check_start-start_time:]
        refv = ref[k][check_start:].to_numpy().transpose()
            # print(refv[0])
            # print(v[9, 40:50])
            # print(refv[9, 40:50])
        bad_count, result = count_unmatched_elements(v, refv, rtol=cur_rtol, atol=cur_atol, equal_nan=True)
        bad_rate = bad_count/ (result.size if result.size else 1)
        tolerance_count = tolerance["bad_count"].get(k, 0.0001)
        if not is_power_of_two(num_stock):
            tolerance_count = max(tolerance_count, tolerance["bad_count_unaligned"].get(k, 0.0002))
        if bad_count:
            if bad_rate < tolerance_count:
                # print("bad count meets the tolerance, skipping")
                continue
            print(k)
            print(f"Unmatched bad_count = {bad_count}/{result.size} ({bad_rate*100:.4f}%) atol={cur_atol} rtol={cur_rtol}")
            done = False
            # print(rng)
            for i in range(num_stock):
                if not np.allclose(v[i], refv[i], rtol=cur_rtol, atol=cur_atol, equal_nan=True):
                    print("Bad stock", i)
                    print("Our output", v[i])
                    print("Ref", refv[i])
                    for j in range(num_time-check_start):
                        if not np.allclose(v[i,j], refv[i,j], rtol=cur_rtol, atol=cur_atol, equal_nan=True):
                            print("j",j,v[i,j], refv[i,j])
                    break
    return done

def test(modu, executor, start_window, num_stock, num_time, my_input, ref, ischeck, start_time):
    # prepare outputs
    outnames = modu.getOutputNames()
    layout = modu.output_layout
    outbuffers = dict()
    print(layout)
    if layout == "TS":
        # Factors, Time, Stock
        sharedbuf = np.empty((len(outnames), num_time-start_time, num_stock), dtype="float32")
        sharedbuf[:] = np.nan
        for idx, name in enumerate(outnames):
            outbuffers[name] = sharedbuf[idx]
    # print(ref.alpha001())
    # blocked = TS_STs(inp)
    
    if not ischeck:
        out = kr.runGraph(executor, modu, my_input, start_time, num_time-start_time, outbuffers)
        start = time.time()
        for _ in range(20):
            out = kr.runGraph(executor, modu, my_input, start_time, num_time-start_time, outbuffers, skip_check = True)
        end = time.time()
        tdiff = (end-start)/20
    else:
        start = time.time()
        out = kr.runGraph(executor, modu, my_input, start_time, num_time-start_time, outbuffers, num_stocks = num_stock)
        end = time.time()
        tdiff = end-start
    print(f"Exec takes: {tdiff:.6f} seconds")
    if not ischeck:
        return True
    # print(out)
    for k in list(out.keys()):
        if layout == "TS":
            out[k] = TS_ST(out[k])
        else:
            out[k] = STs_TS(out[k])
    return check_result(out, ref, outnames, start_window, num_stock, start_time, num_time)

def test_stream(modu, executor, start_window, num_stock, num_time, my_input, ref, ischeck):
    # prepare outputs
    outnames = modu.getOutputNames()
    buffer_id_to_name = [0 * (len(outnames) + len(my_input))]
    buffer_name_to_id = dict()
    stream = kr.StreamContext(executor, modu, num_stock)
    for name in my_input:
        buffer_name_to_id[name] = stream.queryBufferHandle(name)
    outputs = dict()
    for name in outnames:
        buffer_name_to_id[name] = stream.queryBufferHandle(name)
        outputs[name] = np.empty((num_time,num_stock), dtype="float")
    for t in range(num_time):
        for name, value in my_input.items():
            stream.pushData(buffer_name_to_id[name], value[t])
        stream.run()
        for name, value in outputs.items():
            value[t, :] = stream.getCurrentBuffer(buffer_name_to_id[name])
    for name in outputs:
        outputs[name] = TS_ST(outputs[name])
    return check_result(outputs, ref, outnames, start_window, num_stock, 0, num_time)

def streammain():
    modu = lib.getModule("alpha_101_stream")
    start_window = modu.getOutputUnreliableCount()
    # print(start_window)
    num_stock = 64
    num_time = 220
    is_check = True
    my_input, pd_ref = make_data_and_ref(num_stock, num_time, is_check, 0)
    executor = kr.createSingleThreadExecutor()
    done = True
    done = done & test_stream(modu, executor, start_window, num_stock, num_time, my_input, pd_ref, is_check)
    executor = kr.createMultiThreadExecutor(8)
    done = done & test_stream(modu, executor, start_window, num_stock, num_time, my_input, pd_ref, is_check)
    print("OK", done)
    if not done:
        exit(1)


def test64(modu, executor, start_window, num_stock, num_time, my_input, ref, ischeck, start_time):
    # prepare outputs
    outnames = modu.getOutputNames()
    layout = modu.output_layout
    outbuffers = dict()
    print(layout)
    if layout == "TS":
        # Factors, Time, Stock
        sharedbuf = np.empty((len(outnames), num_time-start_time, num_stock), dtype="float64")
        sharedbuf[:] = np.nan
        for idx, name in enumerate(outnames):
            outbuffers[name] = sharedbuf[idx]
    # print(ref.alpha001())
    # blocked = TS_STs(inp)
    if not ischeck:
        out = kr.runGraph(executor, modu, my_input, start_time, num_time-start_time, outbuffers)
        start = time.time()
        for _ in range(20):
            out = kr.runGraph(executor, modu, my_input, start_time, num_time-start_time, outbuffers, skip_check = True)
        end = time.time()
        tdiff = (end-start)/20
    else:
        start = time.time()
        out = kr.runGraph(executor, modu, my_input, start_time, num_time-start_time, outbuffers)
        end = time.time()
        tdiff = end-start
    print(f"Exec takes: {tdiff:.6f} seconds")
    if not ischeck:
        return True
    # print(out)
    for k in list(out.keys()):
        if layout == "TS":
            out[k] = TS_ST(out[k])
        else:
            out[k] = STs_TS(out[k])
    return check_result(out, ref, outnames, start_window, num_stock, start_time, num_time)

def main(is64: bool, is_check: bool):
    modu = lib.getModule("alpha_101" if not is64 else "alpha_101_double")
    start_window = modu.getOutputUnreliableCount()
    num_stock = 64
    done = True
    testfunc = test64 if is64 else test
    blocking_num = modu.blocking_len
    # fp64 version is compiled with TS format
    blocking = 0 if is64 else blocking_num
    def compute():
        nonlocal done
        num_time = 260
        my_input, pd_ref = make_data_and_ref(num_stock, num_time, is_check, blocking, "float64" if is64 else "float32")
        executor = kr.createSingleThreadExecutor()
        done = done & testfunc(modu, executor, start_window, num_stock, num_time, my_input, pd_ref, is_check, 0)
        done = done & testfunc(modu, executor, start_window, num_stock, num_time, my_input, pd_ref, is_check, 50)
        executor = kr.createMultiThreadExecutor(4)
        done = done & testfunc(modu, executor, start_window, num_stock, num_time, my_input, pd_ref, is_check, 0)
    num_stock = 64
    compute()
    # skip benchmarking on unaligned mode
    if not is_check:
        return
    print("Testing unaligned shape")
    num_stock = 64-2
    compute()
    print("OK", done)
    if not done:
        exit(1)

action = sys.argv[1]
def do_compile(avx, keep, tempdir):
    funclist = [check_alpha101(avx),
        check_alpha101_double(avx),
        check_alpha101_stream(avx)]
    if avx == "avx512":
        machine = cfake.X64CPUFlags(avx512=True, avx512dq=True, avx512vl=True)
    else:
        machine = cfake.NativeCPUFlags()
    return cfake.compileit(funclist, "test", cfake.CppCompilerConfig(machine=machine), tempdir=tempdir, keep_files=keep)
if action == "compile_avx512":
    do_compile("avx512", True, "./build")
    exit(0)
elif action == "run_avx512":
    lib = kr.Library.load(os.path.join("./build/test", "test.so"))
else:
    lib = do_compile("avx", False, None)

print("Check f64 batch")
main(True, True)
print("======================================")
print("Check f32 batch")
main(False, True)
print("======================================")
print("Check f32 stream")
streammain()