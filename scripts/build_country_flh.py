#!/usr/bin/env python

import os
import atlite
import numpy as np
import xarray as xr
import pandas as pd

import geokit as gk
from osgeo import gdal
from scipy.sparse import vstack
import pycountry as pyc
import matplotlib.pyplot as plt

from vresutils import landuse as vlanduse
from vresutils.array import spdiag

import progressbar as pgb
import logging
logger = logging.getLogger(__name__)

from build_renewable_profiles import init_globals, calculate_potential

def build_area(flh, countries, areamatrix, breaks, fn):
    area_unbinned = xr.DataArray(areamatrix.todense(), [countries, capacity_factor.coords['spatial']])
    bins = xr.DataArray(pd.cut(flh.to_series(), bins=breaks), flh.coords, name="bins")
    area = area_unbinned.groupby(bins).sum(dim="spatial").to_pandas()
    area.loc[:,slice(*area.sum()[lambda s: s > 0].index[[0,-1]])].to_csv(fn)
    area.columns = area.columns.map(lambda s: s.left)
    return area

def plot_area_not_solar(area, countries):
    # onshore wind/offshore wind
    a = area.T

    fig, axes = plt.subplots(nrows=len(countries), sharex=True)
    for c, ax in zip(countries, axes):
        d = a[[c]] / 1e3
        d.plot.bar(ax=ax, legend=False, align='edge', width=1.)
        ax.set_ylabel(f"Potential {c} / GW")
        ax.set_title(c)
    ax.legend()
    ax.set_xlabel("Full-load hours")
    fig.savefig(snakemake.output.plot, transparent=True, bbox_inches='tight')

def plot_area_solar(area, p_area, countries):
    # onshore wind/offshore wind
    p = p_area.T
    a = area.T

    fig, axes = plt.subplots(nrows=len(countries), sharex=True, squeeze=False)
    for c, ax in zip(countries, axes.flat):
        d = pd.concat([a[c], p[c]], keys=['PyPSA-Eur', 'Pietzker'], axis=1) / 1e3
        d.plot.bar(ax=ax, legend=False, align='edge', width=1.)
        # ax.set_ylabel(f"Potential {c} / GW")
        ax.set_title(c)
    ax.legend()
    ax.set_xlabel("Full-load hours")

    fig.savefig(snakemake.output.plot, transparent=True, bbox_inches='tight')


def build_aggregate(flh, countries, areamatrix, breaks, p_area, fn):
    agg_a = pd.Series(np.ravel((areamatrix / areamatrix.sum(axis=1)).dot(flh.values)),
                            countries, name="PyPSA-Eur")

    if p_area is None:
        agg_a['Overall'] = float((np.asarray((areamatrix.sum(axis=0) / areamatrix.sum())
                                                .dot(flh.values)).squeeze()))

        agg = pd.DataFrame({'PyPSA-Eur': agg_a})
    else:
        # Determine indices of countries which are also in Pietzcker
        inds = pd.Index(countries).get_indexer(p_area.index)
        areamatrix = areamatrix[inds]

        agg_a['Overall'] = float((np.asarray((areamatrix.sum(axis=0) / areamatrix.sum())
                                             .dot(flh.values)).squeeze()))

        midpoints = (breaks[1:] + breaks[:-1])/2.
        p = p_area.T

        # Per-country FLH comparison
        agg_p = pd.Series((p / p.sum()).multiply(midpoints, axis=0).sum(), name="Pietzker")
        agg_p['Overall'] = float((p.sum(axis=1) / p.sum().sum()).multiply(midpoints, axis=0).sum())

        agg = pd.DataFrame({'PyPSA-Eur': agg_a, 'Pietzcker': agg_p, 'Ratio': agg_p / agg_a})

    agg.to_csv(fn)

if __name__ == '__main__':
    # Detect running outside of snakemake and mock snakemake for testing
    if 'snakemake' not in globals():
        from vresutils.snakemake import MockSnakemake, Dict
        snakemake = MockSnakemake(
            wildcards=Dict(technology='solar'),
            input=Dict(
                base_network="networks/base.nc",
                corine="data/bundle/corine/g250_clc06_V18_5.tif",
                natura="resources/natura.tiff",
                gebco="data/bundle/GEBCO_2014_2D.nc",
                country_shapes='resources/country_shapes.geojson',
                offshore_shapes='resources/offshore_shapes.geojson',
                pietzker="data/pietzker2014.xlsx"
            ),
            output=Dict(
                area="resources/country_flh_area_{technology}.csv",
                aggregated="resources/country_flh_aggregated_{technology}.csv",
                uncorrected="resources/country_flh_uncorrected_{technology}.csv",
                plot="resources/country_flh_{technology}.pdf",
                exclusion="resources/country_exclusion_{technology}"
            )
        )
        snakemake.input['regions'] = os.path.join(snakemake.path, "resources",
                                                  "country_shapes.geojson"
                                                  if snakemake.wildcards.technology in ('onwind', 'solar')
                                                  else "offshore_shapes.geojson")
        snakemake.input['cutout'] = os.path.join(snakemake.path, "cutouts",
                                                 snakemake.config["renewable"][snakemake.wildcards.technology]['cutout'])

    pgb.streams.wrap_stderr()
    logging.basicConfig(level=snakemake.config['logging_level'])

    config = snakemake.config['renewable'][snakemake.wildcards.technology]

    time = pd.date_range(freq='m', **snakemake.config['snapshots'])
    params = dict(years=slice(*time.year[[0, -1]]), months=slice(*time.month[[0, -1]]))

    cutout = atlite.Cutout(config['cutout'],
                           cutout_dir=os.path.dirname(snakemake.input.cutout),
                           **params)

    minx, maxx, miny, maxy = cutout.extent
    dx = (maxx - minx) / (cutout.shape[1] - 1)
    dy = (maxy - miny) / (cutout.shape[0] - 1)
    bounds = gk.Extent.from_xXyY((minx - dx/2., maxx + dx/2.,
                                  miny - dy/2., maxy + dy/2.))

    # Use GLAES to compute available potentials and the transition matrix
    paths = dict(snakemake.input)

    init_globals(bounds, dx, dy, config, paths)
    regions = gk.vector.extractFeatures(paths["regions"], onlyAttr=True)
    countries = pd.Index(regions["name"], name="country")

    widgets = [
        pgb.widgets.Percentage(),
        ' ', pgb.widgets.SimpleProgress(format='(%s)' % pgb.widgets.SimpleProgress.DEFAULT_FORMAT),
        ' ', pgb.widgets.Bar(),
        ' ', pgb.widgets.Timer(),
        ' ', pgb.widgets.ETA()
    ]
    progressbar = pgb.ProgressBar(prefix='Compute GIS potentials: ', widgets=widgets, max_value=len(countries))

    if not os.path.isdir(snakemake.output.exclusion):
        os.makedirs(snakemake.output.exclusion)

    matrix = vstack([calculate_potential(i, save_map=os.path.join(snakemake.output.exclusion, countries[i]))
                     for i in progressbar(regions.index)])

    areamatrix = matrix * spdiag(vlanduse._cutout_cell_areas(cutout).ravel())
    areamatrix.data[areamatrix.data < 1.] = 0 # ignore weather cells where only less than 1 km^2 can be installed
    areamatrix.eliminate_zeros()

    resource = config['resource']
    func = getattr(cutout, resource.pop('method'))
    correction_factor = config.get('correction_factor', 1.)

    capacity_factor = func(capacity_factor=True, show_progress='Compute capacity factors: ', **resource).stack(spatial=('y', 'x'))
    flh_uncorr = capacity_factor * 8760
    flh_corr = correction_factor * flh_uncorr

    if snakemake.wildcards.technology == 'solar':
        pietzcker = pd.read_excel(snakemake.input.pietzker, sheet_name="PV on all area", skiprows=2, header=[0,1]).iloc[1:177]
        p_area1_50 = pietzcker['Usable Area at given FLh in 1-50km distance to settlement '].dropna(axis=1)
        p_area1_50.columns = p_area1_50.columns.str.split(' ').str[0]

        p_area50_100 = pietzcker['Usable Area at given FLh in 50-100km distance to settlement ']

        p_area = p_area1_50 + p_area50_100
        cols = p_area.columns
        breaks = cols.str.split('-').str[0].append(pd.Index([cols[-1].split('-')[1]])).astype(int)
        p_area.columns = breaks[:-1]

        p_area = p_area.reindex(countries.map(lambda c: pyc.countries.get(alpha_2=c).name))
        p_area.index = countries
        p_area = p_area.dropna() # Pietzcker does not have data for CZ and MK
    else:
        breaks = np.r_[0:8000:50]
        p_area = None


    area = build_area(flh_corr, countries, areamatrix, breaks, snakemake.output.area)

    if snakemake.wildcards.technology == 'solar':
        plot_area_solar(area, p_area, p_area.index)
    else:
        plot_area_not_solar(area, countries)

    build_aggregate(flh_uncorr, countries, areamatrix, breaks, p_area, snakemake.output.uncorrected)
    build_aggregate(flh_corr, countries, areamatrix, breaks, p_area, snakemake.output.aggregated)
