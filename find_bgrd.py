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


def traverse_and_print_dates(directory, startingday='20230828'):
    for root, dirs, _ in os.walk(directory):
        for name in dirs:
            full_path = os.path.join(root, name)
            data_directory = full_path
            year, month, day = extract_date_from_path(full_path)
            if year and month and day:
                if ''.join([year, month, day]) >= startingday:
                    print("Processing {}".format(full_path))
                    # if True:
                    try:
                        # print(full_path)
                        one_day_proc(full_path)
                    except:
                        print("Error with {}".format(full_path))
                        pass


def one_day_proc(full_path, freq_bin=4, cal_dirs=['/data1/pzhang/lwasolarview/caltables/'],
                 add_logo=True, t1='2024-03-08', t2='2024-03-23', use_synoptic_spec=False,
                 save_dir="/common/lwa/spec/"
                 ):
    if True:
        year, month, day = extract_date_from_path(full_path)

        # compare year, month, day with t1 and t2
        cal_strategy = 0, 1, 2
        if '-'.join([year, month, day]) < t1:
            cal_strategy = 0  # use caltable
        elif '-'.join([year, month, day]) >= t1 and '-'.join([year, month, day]) <= t2:
            cal_strategy = 1  # use caltable and defivde by cal factor 5e4
        elif '-'.join([year, month, day]) > t2:
            cal_strategy = 2  # do not use caltable

        if year and month and day:
            print("[", Time.now().datetime, "], Processing {}".format(full_path))
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

                d.read(files, source='lwa', timebin=4, freqbin=freq_bin, stokes='I', freqrange=[15, 85],
                       flux_factor_file=cal_factor_file,
                       flux_factor_calfac_x=cal_factor_calfac_x,
                       flux_factor_calfac_y=cal_factor_calfac_y)

                # Ensure the output directory exists
                fits_dir = os.path.join(save_dir, 'fits', str(year))
                os.makedirs(fits_dir, exist_ok=True)

                # Save FITS file
                d.tofits(
                    save_dir+'/fits/{}/ovro-lwa.lev1_bmf_256ms_96kHz.{}-{}-{}.dspec_I.fits'.format(year, year, month, day))
            except:
                print(traceback.format_exc())
                print("Error with {}".format(full_path))
                # print error msg
                print( "Error: ", sys.exc_info()[0] )

if __name__ == "__main__":
    """
    This script is used to generate all the spectra for the LWA data

    Example usage:
    python generate_all_spectra.py --oneday
    python generate_all_spectra.py --lasttwoday
    """

    # parse the arg directory path as input
    import argparse
    from datetime import date as ddate
    parser = argparse.ArgumentParser(description='Process some integers.')
    parser.add_argument('datahome', metavar='D', type=str, help='an integer for the accumulator', nargs='?',
                        default='/nas7a/beam/beam-data/')
    parser.add_argument('--oneday', action='store_true',
                        help='Process one day')
    parser.add_argument('--lasttwoday', action='store_true',
                        help='Process the last two days data')
    parser.add_argument('--onedaypath', type=str,
                        help='The data path for one day processing')
    parser.add_argument('--lastnday', type=int,
                        help='Process the last n days data', default=-1)
    parser.add_argument('--runall', action='store_true',
                        help='Process all historical data')
    parser.add_argument('--dir_cal', type=str,
                        help='The directory for calibration factor', default='')
    parser.add_argument('--startingday', type=str,
                        help='The starting day for processing', default='20231012')

    pre_defined_cal_dir = [
        '/data1/pzhang/lwasolarview/caltables/',
    ]

    args = parser.parse_args()
    directory_path = args.datahome
    print(pre_defined_cal_dir)
    print(args.dir_cal)
    pre_defined_cal_dir.append(args.dir_cal)
    print(pre_defined_cal_dir)
    if args.oneday:
        one_day_proc(args.onedaypath)
    elif args.runall:
        traverse_and_print_dates(directory_path, startingday=args.startingday)
    elif args.lasttwoday:
        # get yyyy, mm, dd of today and yesterday
        today = ddate.today()
        yesterday = today - datetime.timedelta(days=1)
        yyyy_today, mm_today, dd_today = today.strftime(
            "%Y"), today.strftime("%m"), today.strftime("%d")
        yyyy_yesterday, mm_yesterday, dd_yesterday = yesterday.strftime(
            "%Y"), yesterday.strftime("%m"), yesterday.strftime("%d")

        one_day_proc(os.path.join(directory_path, yyyy_yesterday+mm_yesterday, 'beam' +
                     yyyy_yesterday+mm_yesterday+dd_yesterday), cal_dirs=pre_defined_cal_dir)
        one_day_proc(os.path.join(directory_path, yyyy_today+mm_today,
                     'beam'+yyyy_today+mm_today+dd_today), cal_dirs=pre_defined_cal_dir)
    elif args.lastnday > 0:
        today = ddate.today()
        for i in range(args.lastnday):
            today = today - datetime.timedelta(days=1)
            yyyy, mm, dd = today.strftime("%Y"), today.strftime(
                "%m"), today.strftime("%d")
            one_day_proc(os.path.join(directory_path, yyyy + mm,
                         'beam' + yyyy + mm + dd), cal_dirs=pre_defined_cal_dir)
