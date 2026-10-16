// Charte Aureon, celle du visage d'ORION (orion/face/static/face.js) : vert forêt (structure), vert secondaire
// (actif), crème (important), doré (accents rares), noir profond (fond). Police du nom : Michroma.

export const colors = {
  background: '#0e0e0e',
  backgroundTop: '#111814',
  deep: 'rgb(15, 61, 46)',
  green: 'rgb(60, 139, 111)',
  mint: 'rgb(128, 186, 160)',
  cream: 'rgb(244, 239, 228)',
  gold: 'rgb(201, 164, 92)',
  muted: 'rgba(244, 239, 228, 0.55)',
  faint: 'rgba(244, 239, 228, 0.28)',
  line: 'rgba(201, 164, 92, 0.28)',
  surface: 'rgb(24, 34, 29)', // bouton secondaire (micro), vert très sombre
  refuse: 'rgb(178, 84, 70)', // terre cuite : refuser, raccrocher (s'accorde au vert et à l'or)
};

export const rgba = (rgb: string, alpha: number) => rgb.replace('rgb(', 'rgba(').replace(')', `, ${alpha})`);

export const fonts = {
  display: 'Michroma_400Regular',
};
