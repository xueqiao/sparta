import astropy.io.fits
import matplotlib.pyplot as plt
import _cupy_numpy as np
import scipy.interpolate
import numexpr as ne
import time
import sys
import os.path
import pdb
import gc
import pdb
import argparse
from scipy.ndimage import uniform_filter
from constants import INSTRUMENT, FILTER, SUBARRAY, TOP_MARGIN, BAD_GRPS, LEFT, RIGHT, TOP, BOT, NONLINEAR_FILE, DARK_FILE, FLAT_FILE, RNOISE_FILE, MASK_FILE, GAIN_FILE, ROTATE, SKIP_SUPERBIAS, SUPERBIAS_FILE, SKIP_FLAT, SKIP_REF, N_REF, BKD_REG_TOP, BKD_REG_BOT

def destripe(data):
    #data: N_int x N_grp x N_row x N_col
    bkd_pixels = np.concatenate((data[:,:,BKD_REG_TOP[0] : BKD_REG_TOP[1]],
                          data[:,:,BKD_REG_BOT[0] : BKD_REG_BOT[1]]),
                         axis=2)
    bkd = np.median(bkd_pixels, axis=2)
    return data - bkd[:,:,np.newaxis,:]
    

def get_mask():
    with astropy.io.fits.open(MASK_FILE) as hdul:
        mask = np.asarray(np.rot90(hdul["DQ"].data, ROTATE)[TOP:BOT, LEFT:RIGHT])
    return mask


def get_gain():
    with astropy.io.fits.open(GAIN_FILE) as hdul:
        substrt_x = int(hdul[0].header["SUBSTRT1"]) - 1
        substrt_y = int(hdul[0].header["SUBSTRT2"]) - 1
        gain = np.asarray(np.rot90(hdul[1].data, ROTATE)[
            TOP-substrt_y : BOT-substrt_y,
            LEFT-substrt_x : RIGHT-substrt_x])
    return gain


def subtract_superbias(data):
    #There are a LOT of pixels with UNRELIABLE_BIAS flag that seem perfectly
    #fine otherwise, so we ignore the superbias DQ
    with astropy.io.fits.open(SUPERBIAS_FILE) as hdul:
        superbias = np.rot90(hdul[1].data, ROTATE)
        if superbias.shape != data.shape[2:]:
            superbias = superbias[TOP:BOT, LEFT:RIGHT]
        
    superbias[np.isnan(superbias)] = 0
    return data - superbias


def subtract_ref(data, noutputs):        
    result = np.copy(data)
    chunk_size = int(data.shape[-1] / hdul[0].header["NOUTPUTS"])

    #Subtract ref along top
    for c in range(int(data.shape[-1]/chunk_size)):
        c_min = c * chunk_size
        c_max = (c + 1) * chunk_size
        mean = np.mean(data[:,:,:N_REF,c_min:c_max], axis=(2,3))
        result[:,:,:,c_min:c_max] -= mean[:,:,np.newaxis,np.newaxis]

    if INSTRUMENT == "NIRSPEC":
        return result

    #Subtract ref along sides
    mean = np.mean(result[:,:,:,:N_REF], axis=3) / 2 + np.mean(result[:,:,:,-N_REF:], axis=3) / 2
    result -= mean[:,:,:,np.newaxis]
    return result


def apply_nonlinearity(data):   
    start = time.time()

    with astropy.io.fits.open(NONLINEAR_FILE) as hdul:
        substrt_x = int(hdul[0].header["SUBSTRT1"]) - 1
        substrt_y = int(hdul[0].header["SUBSTRT2"]) - 1
        coeffs = np.array(np.rot90(hdul[1].data, ROTATE, (-2,-1))[:,
            TOP-substrt_y : BOT-substrt_y,
            LEFT-substrt_x : RIGHT-substrt_x], dtype=np.float64)
        dq = np.array(np.rot90(hdul["DQ"].data, ROTATE)[
            TOP-substrt_y : BOT-substrt_y,
            LEFT-substrt_x : RIGHT-substrt_x])
                      
        result = np.zeros(data.shape, dtype=np.float64)
        exp_data = np.ones(data.shape, dtype=np.float64)
        
        for i in range(len(coeffs)):
            print(i)
            result += coeffs[i] * exp_data
            exp_data *= data
    end = time.time()
    print("Non linearity took", end - start)
    return result, dq > 0


def subtract_dark(data, nframes, groupgap):
    with astropy.io.fits.open(DARK_FILE) as hdul:
        dark = np.array(np.rot90(hdul[1].data, ROTATE, (-2,-1)), dtype=np.float64)
        dq = np.array(np.rot90(hdul["DQ"].data, ROTATE, (-2,-1)))
        if dark.ndim == 4:
            print("Warning: skipping first {} integrations of dark".format(dark.shape[0] - 1))
            dark = dark[-1]
            dq = dq[0,0]
    
    #Make dark frame the right size
    if not (nframes == 1 and groupgap == 0):
        assert(nframes == 1 or (nframes/2).is_integer())
        total_frames = nframes + groupgap
        indices = np.arange(dark.shape[0]) % total_frames
        include = indices < nframes
        trunc_dark = dark[include]
        final_dark = uniform_filter(trunc_dark, [nframes,1,1])[int(nframes/2)::nframes]
    else:
        final_dark = dark
        
    assert(data.shape[1] <= final_dark.shape[0])
    
    result = data - final_dark[:data.shape[1]]
    if INSTRUMENT == "NIRSPEC":
        #NIRSPEC dark mask has a lot of DQ flags, most of which don't seem to be reflected in actual data anomalies
        mask = np.zeros(dq.shape, dtype=bool)
    else:
        mask = dq > 0
    return result, mask


def get_slopes_initial(after_gain, read_noise):
    N_grp = after_gain.shape[1]
    if N_grp > 3 and INSTRUMENT=="MIRI":
        ignore_last = 1
    else:
        ignore_last = 0

    N = N_grp - 1 - ignore_last 
    j = np.array(np.arange(1, N + 1), dtype=np.float64)

    R = read_noise[TOP_MARGIN:]
    cutout = after_gain[:,:N_grp-ignore_last,TOP_MARGIN:] #reject last group
    
    signal_estimate = (cutout[:,-1] - cutout[:, 0]) / N
    error = np.zeros(signal_estimate.shape)
    diff_array = np.diff(cutout, axis=1)
    noise = np.sqrt(2*R[np.newaxis,]**2 + np.absolute(signal_estimate))

    for i in range(len(cutout)):
        ratio_estimate = signal_estimate[i]  / R**2
        ratio_estimate[ratio_estimate < 1e-6] = 1e-6
        l = np.arccosh(1 + ratio_estimate/2)[:, :, np.newaxis]

        #weights = -R[:,:,np.newaxis]**-2 * -np.ones(len(j))
        #weights = -R[:,:,np.newaxis]**-2 * j*(j - N - 1)/2
        weights = -R[:,:,np.newaxis]**-2 * ne.evaluate("exp(l) * (1 - exp(-j*l)) * (exp(j*l - l*N) - exp(l)) / (exp(l) - 1)**2 / (exp(l) + exp(-l*N))")
        #weights = -R[:,:,np.newaxis]**-2 * np.exp(l) * (1 - np.exp(-j*l)) * (np.exp(j*l - l*N) - np.exp(l)) / (np.exp(l) - 1)**2 / (np.exp(l) + np.exp(-l*N))
        #weights[:,:,0:5] = 0
        #weights[:,:,-1] = 0
        signal_estimate[i] = np.sum(diff_array[i].transpose(1,2,0) * weights, axis=2) / np.sum(weights, axis=2)                
        error[i] = 1. / np.sqrt(np.sum(weights, axis=2))

    #Fill in borders in order to maintain size    
    full_signal_estimate = (after_gain[:, -1] - after_gain[:, 0]) / N
    full_signal_estimate[:,TOP_MARGIN:] = signal_estimate
        
    full_error = np.ones(full_signal_estimate.shape) * np.inf
    full_error[:,TOP_MARGIN:] = error

    if N_grp == 2:
        median_residuals = np.zeros(after_gain.shape[1:])
    else:
        residuals = after_gain - (full_signal_estimate*np.arange(N_grp)[:,np.newaxis,np.newaxis,np.newaxis]).transpose((1,0,2,3))
        median_residuals = np.median(residuals, axis=0)
        median_residuals -= np.median(median_residuals, axis=0)

    return full_signal_estimate, full_error, median_residuals


def get_slopes(after_gain, read_noise, max_iter=50, sigma=14, bad_grps=0):
    N_grp = after_gain.shape[1]
    N = N_grp - 1 - bad_grps
    j = np.array(np.arange(1, N + 1), dtype=np.float64)

    R = read_noise[TOP_MARGIN:]
    cutout = after_gain[:,bad_grps:,TOP_MARGIN:]

    diff_array = np.diff(cutout, axis=1)
    signal_estimate = np.clip(np.median(diff_array, axis=1), 0, None)
    error = np.zeros(signal_estimate.shape)
    noise = np.sqrt(2*R[np.newaxis,]**2 + signal_estimate)
    bad_mask = np.zeros(diff_array.shape, dtype=bool)
    pixel_bad_mask = np.zeros(signal_estimate.shape, dtype=bool)

    for iteration in range(max_iter):
        old_bad_mask = np.copy(bad_mask)
        for i in range(len(cutout)):
            ratio_estimate = signal_estimate[i]  / R**2
            ratio_estimate[ratio_estimate < 1e-6] = 1e-6
            l = np.arccosh(1 + ratio_estimate/2)[:, :, np.newaxis]
            #weights = -R[:,:,np.newaxis]**-2 * j*(j - N - 1)/2
            weights = -R[:,:,np.newaxis]**-2 * ne.evaluate("exp(l) * (1 - exp(-j*l)) * (exp(j*l - l*N) - exp(l)) / (exp(l) - 1)**2 / (exp(l) + exp(-l*N))")
            #weights = -R[:,:,np.newaxis]**-2 * np.exp(l) * (1 - np.exp(-j*l)) * (np.exp(j*l - l*N) - np.exp(l)) / (np.exp(l) - 1)**2 / (np.exp(l) + np.exp(-l*N))
            #weights[:,:,:BAD_GRPS] = 0
            #weights[:,:,-1] = 0
            
            #Find cosmic rays and other anomalies
            z_scores = (diff_array[i] - signal_estimate[i, np.newaxis]) / noise[i, np.newaxis]
            bad_mask[i] = np.absolute(z_scores) > sigma
            weights[bad_mask[i].transpose(1,2,0)] = 0
            weights_sum = np.sum(weights, axis=2)
            if np.sum(weights_sum == 0) > 0:
                bad_pos = np.nonzero(weights_sum == 0)
                print("These pixels are bad in integration {}: y,x={}".format(i, bad_pos))
                pixel_bad_mask[i][bad_pos] = True
                weights[bad_pos] += 1

            slightly_bad = np.sum(bad_mask[i], axis=0) > sigma
            pixel_bad_mask[i][slightly_bad] = True
                
            signal_estimate[i] = np.sum(diff_array[i].transpose(1,2,0) * weights, axis=2) / np.sum(weights, axis=2)
            error[i] = 1. / np.sqrt(np.sum(weights, axis=2))

        num_changed = np.sum(old_bad_mask != bad_mask)
        if num_changed == 0:
            break
        print("Num changed", iteration, num_changed)
        gc.collect()

    #Fill in borders in order to maintain size
    full_signal_estimate = (after_gain[:, -1] - after_gain[:, 0]) / N
    full_signal_estimate[:,TOP_MARGIN:] = signal_estimate
        
    full_error = np.ones(full_signal_estimate.shape) * np.inf
    full_error[:,TOP_MARGIN:] = error

    full_pixel_mask = np.zeros(full_signal_estimate.shape, dtype=bool)
    full_pixel_mask[:,TOP_MARGIN:] = pixel_bad_mask

    #Free some memory
    cutout = None
    diff_array = None
    bad_mask = None
    gc.collect()

    if N_grp == 2:
        median_residuals = np.zeros(after_gain.shape)
    else:
        residuals = after_gain - (full_signal_estimate*np.arange(N_grp)[:,np.newaxis,np.newaxis,np.newaxis]).transpose((1,0,2,3))
        median_residuals = np.median(residuals, axis=0)
        median_residuals -= np.median(median_residuals, axis=0)
    return full_signal_estimate, full_error, full_pixel_mask, median_residuals


def set_slopes_saturated(after_gain, signal, grps_to_sat):
    grps_to_sat = np.load(grps_to_sat)
        
    for i in range(after_gain.shape[0]):
        for g in range(after_gain.shape[1] - 1, 1, -1):
            saturated = grps_to_sat <= g
            original = np.copy(signal[i])
            signal[i, saturated] = (after_gain[i, g-1, saturated] - after_gain[i, 0, saturated]) / (g - 1)

            
def apply_flat(signal, error, include_flat_error=False):
    with astropy.io.fits.open(FLAT_FILE) as hdul:
        flat = np.array(np.rot90(hdul["SCI"].data, ROTATE), dtype=np.float64)
        flat_err = np.array(np.rot90(hdul["ERR"].data, ROTATE), dtype=np.float64)

    invalid = np.isnan(flat)
    flat[invalid] = 1
    final_signal = signal / flat
    if include_flat_error:
        final_error = np.sqrt((error / flat)**2 + final_signal**2 * flat_err**2)
    else:
        final_error = error / flat
    final_error[:,invalid] = np.inf
    return final_signal, final_error, flat_err


def get_read_noise(gain):    
    with astropy.io.fits.open(RNOISE_FILE) as hdul:
        substrt_x = int(hdul[0].header["SUBSTRT1"]) - 1
        substrt_y = int(hdul[0].header["SUBSTRT2"]) - 1
        return gain * np.array(np.rot90(hdul[1].data, ROTATE)[
            TOP-substrt_y : BOT-substrt_y,
            LEFT-substrt_x : RIGHT-substrt_x], dtype=np.float64) / np.sqrt(2)

    
def is_power_of_two(n):
    return (n != 0) and (n & (n-1) == 0)

parser = argparse.ArgumentParser()
parser.add_argument("filenames", nargs="+")
parser.add_argument("--median-residuals")
parser.add_argument("--grps-to-sat")
args = parser.parse_args()

for filename in args.filenames:
    print("Processing", filename)
    hdul = astropy.io.fits.open(filename)
    assert(hdul[0].header["INSTRUME"] == INSTRUMENT and hdul[0].header["FILTER"] == FILTER and hdul[0].header["SUBARRAY"] == SUBARRAY)
    
    #Assumptions for dark current subtraction
    nframes = hdul[0].header["NFRAMES"]
    groupgap = hdul[0].header["GROUPGAP"]
    assert(is_power_of_two(nframes))

    data = np.array(
        np.rot90(hdul[1].data, ROTATE, axes=(2,3)),
        dtype=np.float64)
    
    N_int, N_grp, N_row, N_col = data.shape
    mask = get_mask()

    if not SKIP_SUPERBIAS:
        print("Subtracting superbias")
        data = subtract_superbias(data)

    if not SKIP_REF:
        print("Subtracting ref pixels")
        data = subtract_ref(data, hdul[0].header["NOUTPUTS"])

    print("Applying non-linearity correction")
    data, dq = apply_nonlinearity(data)
    mask |= dq
    gc.collect()

    if INSTRUMENT=="NIRSPEC":
        #Do other instruments benefit from this? Haven't checked
        print("Subtracting 1/f noise group by group")
        data = destripe(data)

    print("Applying dark correction")
    data, dark_mask = subtract_dark(data, nframes, groupgap)
    mask |= dark_mask

    print("Applying gain correction")
    gain = get_gain()
    data = data * gain

    read_noise = get_read_noise(gain)
    print("Getting slopes 1")

    #original_data = np.copy(data)
    #data = data[:,0:-1]
    signal, error, residuals1 = get_slopes_initial(data, read_noise)
    if args.median_residuals is not None:
        data -= np.load(args.median_residuals)
        print("Subtracting median residuals")
    else:
        print("Not subtracting median residuals")
        data -= residuals1

    print("Getting slopes 2")
    data[:,:,:,-1] = 0 #sometimes anomalous
    signal, error, per_int_mask, residuals2 = get_slopes(data, read_noise)

    if args.grps_to_sat is not None:
        set_slopes_saturated(data, signal, args.grps_to_sat)
    
    if not SKIP_FLAT:
        print("Applying flat")
        signal, error, flat_err = apply_flat(signal, error)
        
    per_int_mask = per_int_mask | mask
    per_int_mask = per_int_mask | np.isnan(signal)
    sci_hdu = astropy.io.fits.ImageHDU(np.cpu(signal), name="SCI")
    err_hdu = astropy.io.fits.ImageHDU(np.cpu(error), name="ERR")
    dq_hdu = astropy.io.fits.ImageHDU(np.cpu(per_int_mask), name="DQ")
    res1_hdu = astropy.io.fits.ImageHDU(np.cpu(residuals1), name="RESIDUALS1")
    res2_hdu = astropy.io.fits.ImageHDU(np.cpu(residuals2), name="RESIDUALS2")
    read_noise_hdu = astropy.io.fits.ImageHDU(np.cpu(read_noise), name="RNOISE")
    output_hdul = astropy.io.fits.HDUList([hdul[0], sci_hdu, err_hdu, dq_hdu, read_noise_hdu, res1_hdu, res2_hdu, hdul["INT_TIMES"]])
    output_hdul.writeto("rateints_" + os.path.basename(filename).replace("_uncal", ""), overwrite=True)
    output_hdul.close()
    hdul.close()
