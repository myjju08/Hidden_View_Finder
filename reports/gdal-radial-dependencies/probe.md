GDAL 3.8.4 radial dependency experiment

45 scenarios / 180 extreme outside-circle comparisons; inside differences: 0. Four cardinal and four diagonal boundary receivers retained outside-wall visibility and became blocked with an immediate inside predecessor wall. Includes a 5km radius on a 5m grid, 2m data, fractional radii, shifted source cells and curvature 0, 6/7, 1.

Every Edge predecessor decreases one or both absolute source-relative pixel offsets, so its Euclidean center distance is smaller. Range checks precede horizon propagation. By induction, in-radius results depend only on in-radius cells. Full scanline reads do not create horizon dependencies on out-of-range corners. [Official v3.8.4 source](https://github.com/OSGeo/gdal/blob/v3.8.4/alg/viewshed.cpp), lines 59–78, 93–110, 405–493, 544–790. The engine supports square north-up grids and snaps targets to cell centers. This conclusion applies only to the inspected version and mode. Unknown data in the dependency disk still requires rejection. Changing irrelevant corner values for the native input does not fill or repair the source dataset.

These tests establish native dependencies, not exact column LOS or real-data accuracy.
