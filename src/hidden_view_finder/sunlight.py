"""Approximate solar center position from NOAA fractional-year equations.

No atmospheric refraction, local horizon, building shadow or illumination model.
Reference: https://gml.noaa.gov/grad/solcalc/solareqns.PDF
"""
from datetime import datetime, timezone
import calendar
import math


def solar_position(lon: float, lat: float, at: datetime) -> dict:
    if at.tzinfo is None:
        raise ValueError('Solar position needs a timezone-aware datetime')
    utc = at.astimezone(timezone.utc)
    day = utc.timetuple().tm_yday
    hour = utc.hour + utc.minute/60 + utc.second/3600
    gamma = 2*math.pi/(366 if calendar.isleap(utc.year) else 365) * (day-1+(hour-12)/24)
    eq = 229.18*(0.000075+0.001868*math.cos(gamma)-0.032077*math.sin(gamma)
        -0.014615*math.cos(2*gamma)-0.040849*math.sin(2*gamma))
    dec = (0.006918-0.399912*math.cos(gamma)+0.070257*math.sin(gamma)
        -0.006758*math.cos(2*gamma)+0.000907*math.sin(2*gamma)
        -0.002697*math.cos(3*gamma)+0.00148*math.sin(3*gamma))
    ha = math.radians((hour*60+eq+4*lon) % 1440 / 4 - 180)
    phi = math.radians(lat)
    altitude = math.degrees(math.asin(max(-1, min(1,
        math.sin(phi)*math.sin(dec)+math.cos(phi)*math.cos(dec)*math.cos(ha)))))
    azimuth = (math.degrees(math.atan2(math.sin(ha),
        math.cos(ha)*math.sin(phi)-math.tan(dec)*math.cos(phi)))+180) % 360
    return {'status': 'computed', 'model': 'NOAA approximate fractional-year solar center',
            'at': at.isoformat(), 'altitude_deg': round(altitude, 2), 'azimuth_deg': round(azimuth, 2),
            'phase': 'daylight' if altitude > 0 else 'twilight' if altitude > -6 else 'night',
            'limitations': 'Approximate astronomical position; local horizon, clouds and artificial lighting unverified.'}
