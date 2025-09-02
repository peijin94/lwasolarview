# LWA Solar View

A comprehensive toolkit for reducing, processing, and displaying datasets from LWA (Long Wavelength Array) Solar observations.

## Overview

This project provides tools for processing LWA solar observation data, generating spectrograms, and creating a web interface for data visualization. It includes multiple processing modes for different use cases and automated scripts for regular data updates.

## Features

- **Multiple Processing Modes**: Support for different data processing strategies
- **Automated Data Processing**: Scripts for regular data updates
- **Web Interface**: Interactive visualization of solar radio data
- **Calibration Support**: Built-in calibration factor handling
- **Background Processing**: Specialized background file detection and processing
- **Flexible Output**: Support for FITS files and various plot formats

## Installation

1. Clone the repository:
```bash
git clone https://github.com/peijin94/lwasolarview.git
cd lwasolarview
```

2. Set up the conda environment:
```bash
conda activate lwa
```

3. Install required dependencies (ensure you have the necessary packages for LWA data processing)

## Usage

### Main Processing Script

The primary script `generate_all_spectra.py` supports multiple processing modes:

#### Processing Modes

- **Original Mode** (`--mode original`): Full processing with plots and hourly spectrograms
- **Open Mode** (`--mode open`): FITS-only processing for streamlined data generation
- **Background Mode** (`--mode background`): Specialized background file processing

#### Command Line Options

```bash
python generate_all_spectra.py [options]

Options:
  --mode {original,open,background}  Processing mode (default: original)
  --oneday                          Process one day of data
  --lasttwoday                      Process the last two days of data
  --lastnday N                      Process the last N days of data
  --runall                          Process all historical data
  --onedaypath PATH                 Path for one day processing
  --dir_cal PATH                    Directory for calibration factors
  --startingday YYYYMMDD            Starting day for processing (default: 20231012)
  --save_dir PATH                   Custom output directory
  --stokes {I,IV}                   Stokes parameters
  --timebin N                       Time binning factor
  --freq_bin N                      Frequency binning factor (default: 4)
  --add_logo                        Add logos to plots
  --use_synoptic_spec               Use synoptic spectrogram
```

#### Usage Examples

```bash
# Process last two days in original mode (full processing with plots)
python generate_all_spectra.py --mode original --lasttwoday

# Process last two days in open mode (FITS-only)
python generate_all_spectra.py --mode open --lasttwoday

# Process last two days in background mode
python generate_all_spectra.py --mode background --lasttwoday

# Process specific day
python generate_all_spectra.py --mode open --oneday --onedaypath /path/to/data

# Process last 5 days with custom output directory
python generate_all_spectra.py --mode original --lastnday 5 --save_dir /custom/output

# Process all data from a specific date
python generate_all_spectra.py --mode original --runall --startingday 20240101
```

### Automated Processing Scripts

#### Daily Updates

For regular automated processing, use the provided shell scripts:

```bash
# Full processing with plots (original mode)
./run_two_day.sh

# FITS-only processing (open mode)
./run_two_day_open.sh
```

These scripts:
- Activate the conda environment
- Process the last two days of data
- Create a touch file for monitoring (`/data1/pzhang/update_lwaspectra`)

### Web Interface

To run the web interface for data visualization:

```bash
cd website
python webapp.py
```

The web interface provides:
- Interactive spectrogram visualization
- Data browsing capabilities
- Real-time data updates

## Data Processing Details

### Calibration Strategy

The system uses different calibration strategies based on observation dates:

- **Before 2024-03-08**: Uses calibration tables
- **2024-03-08 to 2024-03-23**: Uses calibration tables with 5e4 factor division
- **After 2024-03-23**: No calibration tables used

### Output Structure

#### Original Mode Outputs:
- **FITS files**: `/common/lwa/spec_v2/fits/YYYYMMDD.fits`
- **Daily plots**: `/common/lwa/spec_v2/daily/YYYYMMDD.png`
- **Hourly plots**: `/common/lwa/spec_v2/hourly/YYYYMM/DD_N.png`

#### Open Mode Outputs:
- **FITS files**: `/sbdata/lwa-spec-tmp/spec_lv1/fits/YYYY/ovro-lwa.lev1_bmf_256ms_96kHz.YYYY-MM-DD.dspec_I.fits`

#### Background Mode Outputs:
- **Background FITS files**: `/sbdata/lwa-spec-tmp/spec_lv15/fits_bcgrd/YYYY/ovro-lwa.lev1_bmf_256ms_96kHz.YYYY-MM-DD.dspec_I.fits`

### Background File Detection

In background mode, the system automatically detects background files by:
- Looking for files between 19:00-22:00 UTC
- Finding files with <5 minute gaps to the next file
- Processing only the detected background file

## Configuration

### Calibration Directories

Default calibration directory: `/data1/pzhang/lwasolarview/caltables/`

Additional calibration directories can be specified using the `--dir_cal` option.

### Data Directories

Default data home directory: `/nas7a/beam/beam-data/`

## Monitoring

The system creates a touch file at `/data1/pzhang/update_lwaspectra` when processing completes, which can be used for monitoring and automation.

## Dependencies

Key dependencies include:
- `suncasa`: For solar data processing
- `ovrolwasolar`: For LWA-specific functionality
- `matplotlib`: For plotting
- `astropy`: For time handling
- `numpy`: For numerical operations
