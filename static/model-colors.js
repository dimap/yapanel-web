// One catalogue for the renderer, picker and server validation. Gallery-only concepts keep their palettes.
const response = await fetch(new URL('./model-colors.json', import.meta.url));
if (!response.ok) throw new Error('Не удалось загрузить палитры корпусов');
const catalog = await response.json();
export function modelPalette(id) { return catalog.palettes[catalog.aliases[id] || id]; }
export function normalizeColor(id, value) {
  const key = typeof value === 'string' ? value.trim().toLowerCase().replace(/^gray$/, 'grey') : '';
  return Object.hasOwn(modelPalette(id) || {}, key) ? key : null;
}
