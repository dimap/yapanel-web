# 3D sources and licenses

- Lite 2: `lite2.stl` is the original, unchanged STL «Yandex Station Lite 2 | Яндекс станция лайт 2»
  by **vanyabeat**, https://www.thingiverse.com/thing:7349611 , licensed under
  [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). The panel only loads and colours it in the
  browser (`model-lite2.js`). The MIT license of the panel code does not apply to this file. The STEP source
  is available on the Thingiverse page.
- Station 2, Mini 3 Pro, Mini 3 and Station Lite (1st gen): original approximate visual reconstructions in
  model-studio.js (MIT), based on the manufacturer's public product photographs.
  They are not official CAD, downloaded commercial meshes, or manufacturing drawings.
- Station Mini 2 with clock (YNDX-00020): original approximate reconstruction in model-studio.js (MIT)
  from the owner's photographs and caliper measurements of a white unit, checked against the official
  97 × 54 mm, 260 g specification
  (https://alice.yandex.ru/support/ru/station/meet/characteristics-mini2). Four official colours:
  white, black, red and blue. The clock display uses the panel's shared clock geometry
  (`clock-geometry.js`): 4 × 7 segments, colon, hyphen and degree sign — 32 white LEDs, no decimal points.
  The central Alice diffuser is driven by the native six RGB zones; their mean colour is an approximation.
  The photographs are not bundled.

- Midi: original approximate MIT reconstruction in `model-midi.js`, based on the official
  [96 × 96 × 110 mm specification](https://alice.yandex.ru/support/ru/station/meet/characteristics-midi)
  and [product photographs](https://alice.yandex.ru/station-midi).
  The fabric colour texture and display alpha mask are procedural. Materials use
  MeshStandard/Basic without normal/bump maps, shadows or a ground plane.
  No product photograph, reference photograph or third-party mesh is bundled.
  The 36-value display API visualizes the mapped digits, colon and degree sign;
  the complete native display has 45 white channels. Geometry and light interpolation
  are approximations, not official CAD or a framebuffer codec.

- Gallery placeholders (Max, Station 3, Street, Duo Max, Station, Mini 2 without clock, Mini gen 1,
  all TV Station models and Drops): simplified original shapes in model-concepts.js (MIT) after the
  manufacturer's public photographs. They only help future contributors start; hardware support is not implied.
  Lite 2 without clock reuses lite2.stl with the display off.
  Dimensions, colours and visible features follow the official specification pages:
  https://alice.yandex.ru/support/ru/station/meet/characteristics-max (141 x 141 x 231 mm),
  characteristics-gen3 (132 x 132 x 201 mm), characteristics-midi (96 x 96 x 110 mm),
  characteristics-street (83 mm diameter, 202 mm), characteristics-duo-max (256 x 118 x 240 mm),
  characteristics-gen1 (141 x 141 x 231 mm), characteristics-mini2 (97 x 54 mm), characteristics-mini1 (90 x 45 mm).
  TVs: https://alice.yandex.ru/support/ru/tv/specs/tv-station-led, tv-station-qled, tv-station-miniled,
  tv-station-pro-miniled, tv-station-basic-led, tv-station-basic-qled (size with and without feet, feet spacing).
  Photos: https://alice.yandex.ru/station-max, station-gen3, station-midi, station-street, station-duo-max, station-mini.
  Drops: retailer photos (DNS) of the white 2026 case.

Body palettes for supported models are shared with server validation in `model-colors.json`.
Mini 3 Pro: black, light grey, blue and green, following [Yandex's announcement](https://www.yandex.ru/company/news/02-15-04-2025)
and the [official green product](https://alice.yandex.ru/store/products/station-mini-3-pro-100488).
Material RGB values (including light grey and green) are visual approximations, not calibrated measurements.
An unknown device colour is labelled explicitly; see [body-colour behaviour and tests](../../docs/model-colors.md).

References:
- https://alice.yandex.ru/station-gen2
- https://alice.yandex.ru/station-mini-gen3-pro
- https://alice.yandex.ru/station-mini-gen3
- https://alice.yandex.ru/station-lite

Mini 3 has a lower 102 × 60.5 mm body and central indicator; Mini 3 Pro has a
102 × 71 mm body and perimeter light strip. Mini 2 is a straight 97 × 54 mm fabric cylinder with a flat
perforated top, a large clock behind the fabric on the side and six RGB LEDs behind the central Alice
diffuser; four official colours are selectable in the model settings. Station Lite is an 88 mm puck on a ribbed
black base; the edge slots are decorative, only the Alice logo in the centre lights up (no clock). Details and colours are approximate.
Product photographs are not bundled or used as textures. Models use lightweight materials without normal/bump maps or physical materials.
There are no shadow maps or ground plane. Lite 2
keeps the original main geometry, materials and lighting; colour, auto-rotation and view reset live in
«Настроить» on the «Обзор» page. Three.js and all required assets are local.

The preview page uses explicit demo clock/LED data. This is not a hardware test.
