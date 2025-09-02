import sys
import matplotlib.image as mpimg
import io
import base64
from ovrolwasolar.visualization import njit_logo_str, nsf_logo
import traceback
import glob
from suncasa.utils import ovsas_spectrogram as ovsp
from suncasa.dspec import dspec
import numpy as np
from astropy.time import Time
from mpl_toolkits.axes_grid1 import make_axes_locatable
from matplotlib.dates import AutoDateFormatter, AutoDateLocator, num2date
import datetime
import matplotlib.pyplot as plt
import os
import re

# display off
import matplotlib

matplotlib.use('Agg')

plt.ioff()


def extract_date_from_path(path):
    # Regular expression to match the date pattern in directory names
    match = re.search(r'(\d{4})(\d{2})(\d{2})', path)
    if match:
        year, month, day = match.groups()
        return year, month, day
    return None, None, None


def divide_time_in_hours(time_start, time_end, hour_length=1 / 24):
    # Calculate the total duration in days
    total_duration = time_end.mjd - time_start.mjd

    # Convert duration to hours
    total_hours = total_duration / hour_length
    full_hours = int(total_hours)

    # Check if the last hour is less than 0.5 hours
    if total_hours % 1 < 0.5 and full_hours > 0:
        full_hours -= 1

    # Generate time sections
    time_sections = [(time_start + i * hour_length, time_start +
                      (i + 1) * hour_length) for i in range(full_hours)]

    # Add the remaining time to the last section if there's a remainder
    if total_hours % 1 >= 0.5 or full_hours == 0:
        time_sections.append((time_start + full_hours * hour_length, time_end))

    return time_sections


def rebin1d(arr, new_len):
    shape = (new_len, len(arr) // new_len)
    return arr.reshape(shape).mean(1)


def rebin2d(arr, new_shape):
    shape = (new_shape[0], arr.shape[0] // new_shape[0],
             new_shape[1], arr.shape[1] // new_shape[1])
    return arr.reshape(shape).mean(-1).mean(1)


def get_cal_factor(cal_factor_file):
    import csv
    with open(cal_factor_file, 'r') as f:
        reader = csv.reader(f)
        cal_factors = list(reader)
    freq_num = [a[0] for a in cal_factors[1:]]
    cal_num = [a[1] for a in cal_factors[1:]]
    freq_num = np.array(freq_num, dtype=float)
    cal_num = np.array(cal_num, dtype=float)
    return freq_num, cal_num


def find_background_file(files, year, month, day):
    """Find background file between 19:00-22:00 with 5-minute gap to next file"""
    background_found = False
    background_file = None
    
    for j, file1 in enumerate(files[:-1]):
        hms1 = file1.split('_')[1]
        hms2 = files[j+1].split('_')[1]

        hour1 = int(hms1[0:2])
        minute1 = int(hms1[2:4])
        hour2 = int(hms2[0:2])
        minute2 = int(hms2[2:4])

        if hour1 > 19 and hour1 < 22:
            file_time1 = datetime.datetime(
                int(year), int(month), int(day), hour1, minute1, 0)
            file_time2 = datetime.datetime(
                int(year), int(month), int(day), hour2, minute2, 0)

            if (file_time2 - file_time1).seconds < 300:  # 5 minutes
                background_file = file1
                background_found = True
                print("Background file found: {}, next file: {}".format(file1, files[j+1]))
                break

    return background_file, background_found


def traverse_and_print_dates(directory, startingday='20230828', **kwargs):
    for root, dirs, _ in os.walk(directory):
        for name in dirs:
            full_path = os.path.join(root, name)
            data_directory = full_path
            year, month, day = extract_date_from_path(full_path)
            if year and month and day:
                if ''.join([year, month, day]) >= startingday:
                    print("Processing {}".format(full_path))
                    try:
                        one_day_proc(full_path, **kwargs)
                    except:
                        print("Error with {}".format(full_path))
                        pass


def one_day_proc(full_path, freq_bin=4, cal_dirs=['/data1/pzhang/lwasolarview/caltables/'],
                 add_logo=True, t1='2024-03-08', t2='2024-03-23', use_synoptic_spec=False,
                 mode='original', save_dir=None, stokes='IV', timebin=8):
    """
    Process one day of data with configurable modes
    
    Parameters:
    -----------
    mode : str
        Processing mode: 'original', 'open', or 'background'
    save_dir : str
        Output directory (if None, uses mode-specific defaults)
    stokes : str
        Stokes parameters ('IV' for original, 'I' for open/background)
    timebin : int
        Time binning (8 for original, 4 for open/background)
    """
    
    # Set default save directories based on mode
    if save_dir is None:
        if mode == 'original':
            save_dir = '/common/lwa/spec_v2/'
        elif mode == 'open':
            save_dir = '/sbdata/lwa-spec-tmp/spec_lv1/'
        elif mode == 'background':
            save_dir = '/sbdata/lwa-spec-tmp/spec_lv15/'
    
    year, month, day = extract_date_from_path(full_path)

    # compare year, month, day with t1 and t2
    cal_strategy = 0, 1, 2
    if '-'.join([year, month, day]) < t1:
        cal_strategy = 0  # use caltable
    elif '-'.join([year, month, day]) >= t1 and '-'.join([year, month, day]) <= t2:
        cal_strategy = 1  # use caltable and divide by cal factor 5e4
    elif '-'.join([year, month, day]) > t2:
        cal_strategy = 2  # do not use caltable

    if year and month and day:
        print("[", Time.now().datetime, "], Processing {} in {} mode".format(full_path, mode))
        try:
            str_this_day = ''.join([year, month, day])

            if len(cal_dirs) > 0 and (cal_strategy != 2):  # do calibration
                do_calib = True
                cal_files = []
                for cal_dir in cal_dirs:
                    cal_files += glob.glob(cal_dir + '/*.csv')
                cal_files.sort(key=lambda x: x.split('/')
                               [-1].split('_')[0])

                # find latest cal-list
                date_cal_lst = [cal_factor_csv_f.split('/')[-1].split('_')[0]
                                for cal_factor_csv_f in cal_files]

                idx_cal = 0
                for date_cal in date_cal_lst:
                    if date_cal <= str_this_day:
                        cal_fname = date_cal
                        idx_cal += 1
                    else:
                        break
                print("Using calibration factor from {}".format(
                    cal_files[idx_cal - 1]))
                cal_factor_file = cal_files[idx_cal - 1]
            else:
                cal_factor_file = None

            if cal_strategy == 1:
                cal_factor_calfac_x = 1 / 5e4
                cal_factor_calfac_y = 1 / 5e4
            else:
                cal_factor_calfac_x = 1
                cal_factor_calfac_y = 1

            files = glob.glob(full_path + '/*')
            files.sort()
            d = dspec.Dspec()

            # Handle different processing modes
            if mode == 'background':
                # Find background file
                background_file, background_found = find_background_file(files, year, month, day)
                
                if not background_found:
                    print("No background file found in {}".format(full_path))
                    return  # Exit the function if no background file is found
                
                # Process only background file
                d.read([background_file], source='lwa', timebin=timebin, freqbin=freq_bin, 
                       stokes=stokes, freqrange=[15, 85],
                       flux_factor_file=cal_factor_file,
                       flux_factor_calfac_x=cal_factor_calfac_x,
                       flux_factor_calfac_y=cal_factor_calfac_y)
                
                # Save FITS file for background
                fits_dir = os.path.join(save_dir, 'fits_bcgrd', str(year))
                os.makedirs(fits_dir, exist_ok=True)
                d.tofits(os.path.join(save_dir, 'fits_bcgrd', str(year), 
                                    'ovro-lwa.lev1_bmf_256ms_96kHz.{}-{}-{}.dspec_I.fits'.format(year, month, day)))
                
            else:  # original or open mode
                # Process all files
                d.read(files, source='lwa', timebin=timebin, freqbin=freq_bin, 
                       stokes=stokes, freqrange=[15, 85],
                       flux_factor_file=cal_factor_file,
                       flux_factor_calfac_x=cal_factor_calfac_x,
                       flux_factor_calfac_y=cal_factor_calfac_y)

                # Save FITS file
                if mode == 'original':
                    d.tofits(os.path.join(save_dir, 'fits', '{}{}{}.fits'.format(year, month, day)))
                else:  # open mode
                    fits_dir = os.path.join(save_dir, 'fits', str(year))
                    os.makedirs(fits_dir, exist_ok=True)
                    d.tofits(os.path.join(save_dir, 'fits', str(year), 
                                        'ovro-lwa.lev1_bmf_256ms_96kHz.{}-{}-{}.dspec_I.fits'.format(year, month, day)))

                # Additional processing for original mode
                if mode == 'original':
                    time_range_all = [d.time_axis[0], d.time_axis[-1]]
                    hourly_ranges = divide_time_in_hours(
                        time_range_all[0], time_range_all[1], hour_length=1 / 24)

                    if use_synoptic_spec:
                        ovsp.plot(datetime.datetime(2024, 7, 31), 
                                 figdir=os.path.join(save_dir, 'daily/'), 
                                 figname='{}{}{}.png'.format(year, month, day), 
                                 add_logo=add_logo, combine=True, clip=[10, 99.5])
                    else:
                        fig = d.plot(pol='I', minmaxpercentile=True, vmax2=0.5, vmin2=-0.5,
                                     freq_unit="MHz", plot_fast=True)
                        ax = fig.get_axes()[0]
                        locator = AutoDateLocator(minticks=2)
                        ax.xaxis.set_major_locator(locator)
                        formatter = AutoDateFormatter(locator)
                        formatter.scaled[1 / 24] = '%H:%M'
                        formatter.scaled[1 / (24 * 60)] = '%H:%M'
                        ax.xaxis.set_major_formatter(formatter)
                        ax.set_title(d.time_axis[0].strftime('%Y-%m-%d %H:%M:%S') + ' - ' + 
                                   d.time_axis[-1].strftime('%Y-%m-%d %H:%M:%S'))

                        if add_logo:
                            ax_logo1 = fig.add_axes([0.89, 0.91, 0.15, 0.08])
                            img1 = base64.b64decode(njit_logo_str)
                            img1 = io.BytesIO(img1)
                            img1 = mpimg.imread(img1, format='png')
                            ax_logo1.imshow(img1)
                            ax_logo1.axis('off')

                            ax_logo2 = fig.add_axes([0.81, 0.91, 0.15, 0.09])
                            img2 = base64.b64decode(nsf_logo)
                            img2 = io.BytesIO(img2)
                            img2 = mpimg.imread(img2, format='png')
                            ax_logo2.imshow(img2)
                            ax_logo2.axis('off')

                        fig.savefig(os.path.join(save_dir, 'daily', '{}{}{}.png'.format(year, month, day)))

                    # Generate hourly plots
                    for i in range(len(hourly_ranges)):
                        try:
                            thishour = [hourly_ranges[i][0].datetime.strftime('%Y-%m-%dT%H:%M:%S'),
                                        hourly_ranges[i][1].datetime.strftime('%Y-%m-%dT%H:%M:%S')]
                            fig = d.plot(pol='IP', timerange=thishour, freq_unit="MHz",
                                         plot_fast=True, minmaxpercentile=True,
                                         vmax2=0.5, vmin2=-0.5)
                            fig.suptitle(thishour[0], y=1.02)
                            hourly_dir = os.path.join(save_dir, 'hourly', '{}{}'.format(year, month))
                            os.makedirs(hourly_dir, exist_ok=True)
                            fig.savefig(os.path.join(hourly_dir, '{}_{}.png'.format(day, i)), bbox_inches='tight')
                            plt.close(fig)
                        except:
                            print(traceback.format_exc())
                            print("Error with hourly plot for {}".format(full_path))
                            print("Error: ", sys.exc_info()[0])

        except:
            print(traceback.format_exc())
            print("Error with {}".format(full_path))
            print("Error: ", sys.exc_info()[0])


if __name__ == "__main__":
    """
    Combined script for generating LWA spectra with multiple processing modes
    
    Processing modes:
    - original: Full processing with plots (like generate_all_spectra.py)
    - open: FITS-only processing (like generate_all_spectra_open.py)  
    - background: Background file processing (like generate_all_spectra_open_bg.py)
    
    Example usage:
    python generate_all_spectra_combined.py --mode original --lasttwoday
    python generate_all_spectra_combined.py --mode open --oneday --onedaypath /path/to/data
    python generate_all_spectra_combined.py --mode background --lastnday 3
    """

    import argparse
    from datetime import date as ddate
    
    parser = argparse.ArgumentParser(description='Combined LWA spectra generation script')
    parser.add_argument('datahome', metavar='D', type=str, help='Data home directory', nargs='?',
                        default='/nas7a/beam/beam-data/')
    parser.add_argument('--mode', type=str, choices=['original', 'open', 'background'],
                        default='original', help='Processing mode')
    parser.add_argument('--oneday', action='store_true', help='Process one day')
    parser.add_argument('--lasttwoday', action='store_true', help='Process the last two days data')
    parser.add_argument('--onedaypath', type=str, help='The data path for one day processing')
    parser.add_argument('--lastnday', type=int, help='Process the last n days data', default=-1)
    parser.add_argument('--runall', action='store_true', help='Process all historical data')
    parser.add_argument('--dir_cal', type=str, help='The directory for calibration factor', default='')
    parser.add_argument('--startingday', type=str, help='The starting day for processing', default='20231012')
    parser.add_argument('--save_dir', type=str, help='Custom output directory')
    parser.add_argument('--stokes', type=str, help='Stokes parameters', default=None)
    parser.add_argument('--timebin', type=int, help='Time binning', default=None)
    parser.add_argument('--freq_bin', type=int, help='Frequency binning', default=4)
    parser.add_argument('--add_logo', action='store_true', help='Add logos to plots')
    parser.add_argument('--use_synoptic_spec', action='store_true', help='Use synoptic spectrogram')

    pre_defined_cal_dir = ['/data1/pzhang/lwasolarview/caltables/']

    args = parser.parse_args()
    directory_path = args.datahome
    
    # Set mode-specific defaults
    if args.stokes is None:
        if args.mode == 'original':
            args.stokes = 'IV'
        else:  # open or background
            args.stokes = 'I'
    
    if args.timebin is None:
        if args.mode == 'original':
            args.timebin = 8
        else:  # open or background
            args.timebin = 4
    
    # Add custom calibration directory
    if args.dir_cal:
        pre_defined_cal_dir.append(args.dir_cal)
    
    print("Processing mode: {}".format(args.mode))
    print("Calibration directories: {}".format(pre_defined_cal_dir))
    
    # Prepare kwargs for one_day_proc
    proc_kwargs = {
        'freq_bin': args.freq_bin,
        'cal_dirs': pre_defined_cal_dir,
        'add_logo': args.add_logo,
        'mode': args.mode,
        'save_dir': args.save_dir,
        'stokes': args.stokes,
        'timebin': args.timebin,
        'use_synoptic_spec': args.use_synoptic_spec
    }
    
    if args.oneday:
        one_day_proc(args.onedaypath, **proc_kwargs)
    elif args.runall:
        traverse_and_print_dates(directory_path, startingday=args.startingday, **proc_kwargs)
    elif args.lasttwoday:
        # get yyyy, mm, dd of today and yesterday
        today = ddate.today()
        yesterday = today - datetime.timedelta(days=1)
        yyyy_today, mm_today, dd_today = today.strftime("%Y"), today.strftime("%m"), today.strftime("%d")
        yyyy_yesterday, mm_yesterday, dd_yesterday = yesterday.strftime("%Y"), yesterday.strftime("%m"), yesterday.strftime("%d")

        one_day_proc(os.path.join(directory_path, yyyy_yesterday+mm_yesterday, 'beam' +
                     yyyy_yesterday+mm_yesterday+dd_yesterday), **proc_kwargs)
        one_day_proc(os.path.join(directory_path, yyyy_today+mm_today,
                     'beam'+yyyy_today+mm_today+dd_today), **proc_kwargs)
    elif args.lastnday > 0:
        today = ddate.today()
        for i in range(args.lastnday):
            today = today - datetime.timedelta(days=1)
            yyyy, mm, dd = today.strftime("%Y"), today.strftime("%m"), today.strftime("%d")
            one_day_proc(os.path.join(directory_path, yyyy + mm,
                         'beam' + yyyy + mm + dd), **proc_kwargs)
