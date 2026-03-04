"""
plot_spec_fits.py  --  Plot a two-panel dynamic spectrum from an LWA FITS file.

Upper panel : Stokes I (flux density)
Lower panel : V/I   (fractional circular polarisation, -1 ... +1)

Usage
-----
    python plot_spec_fits.py /common/lwa/spec_v2/fits/20230719.fits
    python plot_spec_fits.py 20230719.fits --outdir /tmp/plots --vmax 200 --pct 99.5
"""

import argparse
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from astropy.io import fits
from astropy.time import Time
from matplotlib.dates import AutoDateFormatter, AutoDateLocator


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def load_fits(path):
    """Return (spec_I, spec_V, freqs_MHz, times_datetime) from an LWA FITS file."""
    with fits.open(path) as f:
        data  = f[0].data                        # (2, 1, nfreq, ntime)  float64
        freqs = f[1].data['sfreq'].astype(float) # GHz
        ut    = f[2].data
        mjd   = (ut['mjd'].astype(float)
                 + ut['time'].astype(float) / 1000. / 86400.)

    spec_I = data[0, 0]   # (nfreq, ntime)
    spec_V = data[1, 0]   # (nfreq, ntime)
    freqs_MHz = freqs * 1e3
    times = Time(mjd, format='mjd').datetime

    return spec_I, spec_V, freqs_MHz, times


def robust_vmin_vmax(arr, pct_lo=0.5, pct_hi=99.5):
    """Return (vmin, vmax) clipped to percentiles, ignoring NaN."""
    lo = np.nanpercentile(arr, pct_lo)
    hi = np.nanpercentile(arr, pct_hi)
    return lo, hi


# ---------------------------------------------------------------------------
# main plot
# ---------------------------------------------------------------------------

def plot_spectrum(fits_path, outdir='./tmp_plt',
                 vmax_I=None, pct_hi_I=99,
                 vi_range=(-0.3, 0.3)):
    """
    Parameters
    ----------
    fits_path : str
        Path to the LWA FITS spectrum file.
    outdir : str
        Directory where the PNG will be saved.
    vmax_I : float or None
        Fixed colour-scale maximum for Stokes I.  If None, use percentile.
    pct_hi_I : float
        Upper percentile for auto Stokes-I colour scale (default 99.5).
    vi_range : (float, float)
        Colour-scale limits for V/I panel (default –1 … +1).
    """
    os.makedirs(outdir, exist_ok=True)

    spec_I, spec_V, freqs_MHz, times = load_fits(fits_path)

    # --- V/I (clip I to avoid divide-by-zero / noise blow-up) ---------------
    I_floor = max(np.nanpercentile(spec_I, 10), 1e-6)
    #spec_I_safe = np.where(spec_I > I_floor, spec_I, np.nan)
    spec_VI = spec_V / spec_I

    # --- colour scales -------------------------------------------------------
    vmin_I = max(np.nanpercentile(spec_I, 3), 0.)
    vmax_I = vmax_I if vmax_I is not None else np.nanpercentile(spec_I, pct_hi_I)
    vi_lo, vi_hi = vi_range

    # --- figure --------------------------------------------------------------
    fig, axes = plt.subplots(2, 1, figsize=(14, 7),
                             sharex=True,
                             gridspec_kw={'hspace': 0.08})

    extent = [times[0], times[-1], freqs_MHz[0], freqs_MHz[-1]]

    # upper: Stokes I
    ax_I = axes[0]
    im_I = ax_I.imshow(spec_I,
                       aspect='auto', origin='lower',
                       extent=extent,
                       vmin=vmin_I, vmax=vmax_I,
                       cmap='inferno',
                       interpolation='nearest')
    ax_I.set_ylabel('Frequency (MHz)', fontsize=11)
    ax_I.set_title('Stokes I', fontsize=11, loc='left')
    cb_I = fig.colorbar(im_I, ax=ax_I, pad=0.01, fraction=0.03)
    cb_I.set_label('Flux density (Jy)', fontsize=9)

    # lower: V/I
    ax_VI = axes[1]
    im_VI = ax_VI.imshow(spec_VI,
                         aspect='auto', origin='lower',
                         extent=extent,
                         vmin=vi_lo, vmax=vi_hi,
                         cmap='RdBu_r',
                         interpolation='nearest')
    ax_VI.set_ylabel('Frequency (MHz)', fontsize=11)
    ax_VI.set_xlabel('Time (UT)', fontsize=11)
    ax_VI.set_title('V / I', fontsize=11, loc='left')
    cb_VI = fig.colorbar(im_VI, ax=ax_VI, pad=0.01, fraction=0.03)
    cb_VI.set_label('V / I', fontsize=9)

    # x-axis time formatting
    locator = AutoDateLocator(minticks=4, maxticks=10)
    formatter = AutoDateFormatter(locator)
    formatter.scaled[1 / 24]        = '%H:%M'
    formatter.scaled[1 / (24 * 60)] = '%H:%M'
    ax_VI.xaxis.set_major_locator(locator)
    ax_VI.xaxis.set_major_formatter(formatter)
    fig.autofmt_xdate(rotation=0, ha='center')

    # overall title
    date_str = Time(times[0]).strftime('%Y-%m-%d')
    t_start  = Time(times[0]).strftime('%H:%M:%S')
    t_end    = Time(times[-1]).strftime('%H:%M:%S')
    fig.suptitle('OVRO-LWA  {}  {}–{} UT'.format(date_str, t_start, t_end),
                 fontsize=12, y=1.01)

    # --- save ----------------------------------------------------------------
    basename = os.path.splitext(os.path.basename(fits_path))[0]
    outpath  = os.path.join(outdir, '{}_spec.png'.format(basename))
    fig.savefig(outpath, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print('Saved to {}'.format(outpath))
    return outpath


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Plot Stokes-I and V/I dynamic spectra from an LWA FITS file.')
    parser.add_argument('fits_file', type=str,
                        help='Path to the LWA FITS spectrum file')
    parser.add_argument('--outdir', type=str, default='./tmp_plt',
                        help='Output directory for the PNG (default: ./tmp_plt)')
    parser.add_argument('--vmax', type=float, default=None,
                        help='Fixed colour-scale maximum for Stokes I (Jy); '
                             'if omitted, uses --pct percentile')
    parser.add_argument('--pct', type=float, default=99,
                        help='Upper percentile for Stokes-I auto colour scale '
                             '(default: 99)')
    parser.add_argument('--vi_min', type=float, default=-0.2,
                        help='Colour-scale minimum for V/I panel (default: -0.2)')
    parser.add_argument('--vi_max', type=float, default=0.2,
                        help='Colour-scale maximum for V/I panel (default: +0.2)')

    args = parser.parse_args()

    plot_spectrum(
        fits_path=args.fits_file,
        outdir=args.outdir,
        vmax_I=args.vmax,
        pct_hi_I=args.pct,
        vi_range=(args.vi_min, args.vi_max),
    )
