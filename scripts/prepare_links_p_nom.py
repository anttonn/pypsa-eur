#!/usr/bin/env python

import pandas as pd
import numpy as np

links_p_nom = pd.read_html('https://en.wikipedia.org/wiki/List_of_HVDC_projects', header=0, match="SwePol")[0]

def extract_coordinates(s):
    regex = (r"(\d{1,2})°(\d{1,2})′(\d{1,2})″(N|S) "
             r"(\d{1,2})°(\d{1,2})′(\d{1,2})″(E|W)")
    e = s.str.extract(regex, expand=True)
    lat = (e[0].astype(float) + (e[1].astype(float) + e[2].astype(float)/60.)/60.)*e[3].map({'N': +1., 'S': -1.})
    lon = (e[4].astype(float) + (e[5].astype(float) + e[6].astype(float)/60.)/60.)*e[7].map({'E': +1., 'W': -1.})
    return lon, lat

m_b = links_p_nom["Power (MW)"].str.contains('x').fillna(False)
def multiply(s): return s.str[0].astype(float) * s.str[1].astype(float)
links_p_nom.loc[m_b, "Power (MW)"] = links_p_nom.loc[m_b, "Power (MW)"].str.split('x').pipe(multiply)
links_p_nom["Power (MW)"] = links_p_nom["Power (MW)"].str.extract("[-/]?([\d.]+)", expand=False).astype(float)

links_p_nom['x1'], links_p_nom['y1'] = extract_coordinates(links_p_nom['Converterstation 1'])
links_p_nom['x2'], links_p_nom['y2'] = extract_coordinates(links_p_nom['Converterstation 2'])

links_p_nom.dropna(subset=['x1', 'y1', 'x2', 'y2']).to_csv(snakemake.output[0], index=False)

