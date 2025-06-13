from flask import Flask, render_template, request, send_from_directory
import os
import ephem
from datetime import datetime, timedelta

import pytz
app = Flask(__name__)
from flask import jsonify

EXTERNAL_IMAGES_FOLDER = '/common/lwa/spec_v2/'
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get-avail-day')
def get_avail_day():
    # from list of file in daily folder, get the dates
    # return the dates
    available_dates = []
    for f in os.listdir(EXTERNAL_IMAGES_FOLDER + 'daily/'):
        fname = (f.split('.')[0])
        available_dates.append(f'{fname[0:4]}-{fname[4:6]}-{fname[6:8]}')
    print(available_dates)
    return jsonify(available_dates)



@app.route('/get-image', methods=['POST'])
def get_image():
    date = request.form['date']
    # Function to find multiple image paths based on the date
    image_paths = find_images_for_date(date)

    print(image_paths)
    # Construct URLs for the images
    image_urls = [f'/lwa/extm/{path}' for path in image_paths]
    return jsonify({'image_urls': image_urls})


@app.route('/extm/<path:filename>')
def get_external_image(filename):
    print(filename)
    return send_from_directory(EXTERNAL_IMAGES_FOLDER, filename)

from glob import glob

def find_images_for_date(date, use_synoptic_spec=False):
    # Split the date into year, month, and day
    yyyy, mm, dd = date.split('-')

    # Construct the file path
    daily_fname = f'{EXTERNAL_IMAGES_FOLDER}daily/{yyyy}{mm}{dd}.png'

    print(daily_fname)
    # Check if the file exists

    image_paths = []

    if os.path.exists(daily_fname):
        # If the file exists, return the path relative to EXTERNAL_IMAGES_FOLDER
        if use_synoptic_spec:
            image_paths.append( 'daily/' + f'fig-OVSAs_spec_{yyyy}{mm}{dd}.png')
            # https://ovsa.njit.edu/SynopticImg/eovsamedia/eovsa-browser/2025/05/31/fig-OVSAs_spec_20250531.jpg
        else:
            image_paths.append( 'daily/' + f'{yyyy}{mm}{dd}.png')
        hourly_dir = f'{EXTERNAL_IMAGES_FOLDER}hourly/{yyyy}{mm}/'
        hourly_files = glob(hourly_dir + f'/{dd}_*.png')
        image_paths.extend( [ f'hourly/{yyyy}{mm}/' + os.path.basename(f) for f in hourly_files])
        print(image_paths)
        return image_paths

    else:
        # If the file doesn't exist, you can return a default image or an error
        return []  # Replace with your default image path


@app.route('/ephm')
def ephemeris():
    # Create OVRO observer
    ovro = ephem.Observer()
    ovro.lat = '37.2332'
    ovro.lon = '-118.2872'
    ovro.elevation = 1222  # meters

    # Get current UTC time
    current_utc = datetime.now(pytz.UTC)
    ovro.date = current_utc

    # Calculate sun position
    sun = ephem.Sun()
    sun.compute(ovro)

    alt_deg = float(sun.alt) * 180 / ephem.pi
    az_deg = float(sun.az) * 180 / ephem.pi

    # Sunrise/sunset
    next_sunrise = ovro.next_rising(sun).datetime()
    next_sunset = ovro.next_setting(sun).datetime()
    prev_sunrise = ovro.previous_rising(sun).datetime()
    prev_sunset = ovro.previous_setting(sun).datetime()

    sunrise_time = prev_sunrise if prev_sunrise.date() == current_utc.date() else next_sunrise
    #sunset_time = prev_sunset if prev_sunset.date() == current_utc.date() else next_sunset
    sunset_time = next_sunset

    # Helper to find when altitude crosses 12°
    def find_altitude_crossing(observer, start_time, direction='rising', target_alt_deg=12.0):
        observer.date = start_time
        step = ephem.minute  # 1-minute step
        max_minutes = 120  # Look within next 2 hours

        for _ in range(max_minutes):
            sun.compute(observer)
            alt = float(sun.alt) * 180 / ephem.pi
            if (direction == 'rising' and alt >= target_alt_deg) or (direction == 'setting' and alt <= target_alt_deg):
                return observer.date.datetime()
            observer.date += step
        return None  # fallback

    # Compute 12° crossings
    sun_12up = find_altitude_crossing(ovro, sunrise_time, direction='rising')
    sun_12down = find_altitude_crossing(ovro, sunset_time - timedelta(hours=2), direction='setting')

    return render_template('ephemeris.html',
                           current_time=current_utc.strftime('%y-%m-%d %H:%M:%S'),
                           alt=f"{alt_deg:.1f}°",
                           az=f"{az_deg:.1f}°",
                           sunrise=sunrise_time.strftime('%y-%m-%d %H:%M:%S'),
                           sunset=sunset_time.strftime('%y-%m-%d %H:%M:%S'),
                           sun_12up=sun_12up.strftime('%y-%m-%d %H:%M:%S') if sun_12up else "N/A",
                           sun_12down=sun_12down.strftime('%y-%m-%d %H:%M:%S') if sun_12down else "N/A")

if __name__ == '__main__':
    from waitress import serve
    serve(app, host="127.0.0.1", port=5001)
