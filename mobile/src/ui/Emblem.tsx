// Emblème d'ORION : version mobile du visage (cercles fins, axes, anneau doré ouvert à point crème, larges arcs vert
// forêt, graduations). Chaque couche tourne à son rythme ; « sonnerie » : plus vif et pulsé ; « appel » : animé.

import { useEffect, useRef } from 'react';
import { Animated, Easing, StyleSheet, Text, View } from 'react-native';
import Svg, { Circle, Defs, G, Line, LinearGradient, Path, RadialGradient, Stop } from 'react-native-svg';

import { colors, fonts, rgba } from './theme';

export type EmblemMode = 'veille' | 'sonnerie' | 'appel';

const TAU = Math.PI * 2;

function arcPath(cx: number, cy: number, r: number, a0: number, a1: number) {
  const x0 = cx + Math.cos(a0) * r;
  const y0 = cy + Math.sin(a0) * r;
  const x1 = cx + Math.cos(a1) * r;
  const y1 = cy + Math.sin(a1) * r;
  const large = a1 - a0 > Math.PI ? 1 : 0;
  return `M ${x0} ${y0} A ${r} ${r} 0 ${large} 1 ${x1} ${y1}`;
}

function useSpin(seconds: number, direction: 1 | -1) {
  const value = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    value.setValue(0);
    const loop = Animated.loop(
      Animated.timing(value, { toValue: 1, duration: seconds * 1000, easing: Easing.linear, useNativeDriver: true }),
    );
    loop.start();
    return () => loop.stop();
  }, [seconds, value]);
  return value.interpolate({ inputRange: [0, 1], outputRange: ['0deg', `${direction * 360}deg`] });
}

function usePulse(active: boolean, period: number) {
  const value = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    if (!active) {
      value.setValue(0);
      return;
    }
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(value, { toValue: 1, duration: period / 2, easing: Easing.inOut(Easing.sin), useNativeDriver: true }),
        Animated.timing(value, { toValue: 0, duration: period / 2, easing: Easing.inOut(Easing.sin), useNativeDriver: true }),
      ]),
    );
    loop.start();
    return () => loop.stop();
  }, [active, period, value]);
  return value;
}

function Layer({ size, rotate, children }: { size: number; rotate?: Animated.AnimatedInterpolation<string>; children: React.ReactNode }) {
  return (
    <Animated.View style={[StyleSheet.absoluteFill, rotate ? { transform: [{ rotate }] } : null]}>
      <Svg width={size} height={size}>{children}</Svg>
    </Animated.View>
  );
}

export function Emblem({ size, mode = 'veille', showName = true }: { size: number; mode?: EmblemMode; showName?: boolean }) {
  const c = size / 2;
  const R = size * 0.4;
  const pace = mode === 'sonnerie' ? 0.35 : mode === 'appel' ? 0.6 : 1;
  const outer = useSpin(110 * pace, -1);
  const gold = useSpin(80 * pace, 1);
  const ticks = useSpin(140 * pace, -1);
  const inner = useSpin(60 * pace, 1);
  const pulse = usePulse(mode !== 'veille', mode === 'sonnerie' ? 1400 : 2600);
  const glowOpacity = pulse.interpolate({ inputRange: [0, 1], outputRange: [mode === 'veille' ? 0.55 : 0.6, 1] });
  const scale = pulse.interpolate({ inputRange: [0, 1], outputRange: [1, mode === 'sonnerie' ? 1.035 : 1.012] });

  const tickMarks = Array.from({ length: 72 }, (_, i) => {
    const a = (i / 72) * TAU;
    const major = i % 6 === 0;
    const r0 = R * 0.75;
    const r1 = R * (major ? 0.705 : 0.725);
    return (
      <Line key={i} x1={c + Math.cos(a) * r0} y1={c + Math.sin(a) * r0} x2={c + Math.cos(a) * r1} y2={c + Math.sin(a) * r1}
        stroke={major ? colors.cream : colors.mint} strokeOpacity={major ? 0.55 : 0.3} strokeWidth={major ? 1.4 : 1} />
    );
  });

  return (
    <Animated.View style={{ width: size, height: size, transform: [{ scale }] }}>
      {/* Lueur de fond, vert profond vers le noir. */}
      <Animated.View style={[StyleSheet.absoluteFill, { opacity: glowOpacity }]}>
        <Svg width={size} height={size}>
          <Defs>
            <RadialGradient id="glow" cx="50%" cy="50%" r="50%">
              <Stop offset="0" stopColor={colors.deep} stopOpacity={0.55} />
              <Stop offset="0.6" stopColor={colors.deep} stopOpacity={0.18} />
              <Stop offset="1" stopColor={colors.background} stopOpacity={0} />
            </RadialGradient>
          </Defs>
          <Circle cx={c} cy={c} r={c} fill="url(#glow)" />
        </Svg>
      </Animated.View>

      {/* Axes horizontal et vertical, en fondu doré vers l'extérieur (fixes). */}
      <Layer size={size}>
        <Defs>
          <LinearGradient id="axisL" x1="0" y1="0" x2="1" y2="0">
            <Stop offset="0" stopColor={colors.gold} stopOpacity={0} />
            <Stop offset="1" stopColor={colors.gold} stopOpacity={0.45} />
          </LinearGradient>
          <LinearGradient id="axisR" x1="0" y1="0" x2="1" y2="0">
            <Stop offset="0" stopColor={colors.gold} stopOpacity={0.45} />
            <Stop offset="1" stopColor={colors.gold} stopOpacity={0} />
          </LinearGradient>
          <LinearGradient id="axisT" x1="0" y1="0" x2="0" y2="1">
            <Stop offset="0" stopColor={colors.gold} stopOpacity={0} />
            <Stop offset="1" stopColor={colors.gold} stopOpacity={0.45} />
          </LinearGradient>
          <LinearGradient id="axisB" x1="0" y1="0" x2="0" y2="1">
            <Stop offset="0" stopColor={colors.gold} stopOpacity={0.45} />
            <Stop offset="1" stopColor={colors.gold} stopOpacity={0} />
          </LinearGradient>
        </Defs>
        <Path d={`M 0 ${c} H ${c - R * 0.62}`} stroke="url(#axisL)" strokeWidth={1} />
        <Path d={`M ${c + R * 0.62} ${c} H ${size}`} stroke="url(#axisR)" strokeWidth={1} />
        <Path d={`M ${c} 0 V ${c - R * 0.62}`} stroke="url(#axisT)" strokeWidth={1} />
        <Path d={`M ${c} ${c + R * 0.62} V ${size}`} stroke="url(#axisB)" strokeWidth={1} />
        <Circle cx={c} cy={c} r={R * 1.12} stroke={colors.cream} strokeOpacity={0.07} strokeWidth={1} fill="none" />
      </Layer>

      {/* Larges arcs vert forêt (signature extérieure), en alternance avec le vert clair. */}
      <Layer size={size} rotate={outer}>
        {[[-1.25, -0.42, false], [2.75, 3.55, false], [1.75, 2.45, true], [0.35, 1.05, true]].map(([a0, a1, second], i) => (
          <G key={i}>
            <Path d={arcPath(c, c, R * 0.98, a0 as number, a1 as number)} stroke={second ? colors.mint : colors.deep}
              strokeOpacity={second ? 0.26 : 0.75} strokeWidth={R * 0.075} strokeLinecap="round" fill="none" />
            <Path d={arcPath(c, c, R * 0.98, a0 as number, a1 as number)} stroke={second ? colors.mint : colors.green}
              strokeOpacity={second ? 0.4 : 0.5} strokeWidth={R * 0.035} strokeLinecap="round" fill="none" />
          </G>
        ))}
      </Layer>

      {/* Anneau doré ouvert sur un côté, point crème à son sommet. */}
      <Layer size={size} rotate={gold}>
        <Path d={arcPath(c, c, R * 0.86, -Math.PI / 2 + 0.35, -Math.PI / 2 + TAU - 0.95)} stroke={colors.gold}
          strokeOpacity={0.85} strokeWidth={1.6} strokeLinecap="round" fill="none" />
        <Path d={arcPath(c, c, R * 0.83, -Math.PI / 2 + 0.6, -Math.PI / 2 + 2.2)} stroke={colors.gold}
          strokeOpacity={0.4} strokeWidth={1} fill="none" />
        <Circle cx={c} cy={c - R * 0.86} r={R * 0.022} fill={colors.cream} />
        <Circle cx={c} cy={c - R * 0.86} r={R * 0.06} fill={colors.cream} fillOpacity={0.12} />
      </Layer>

      {/* Graduations. */}
      <Layer size={size} rotate={ticks}>{tickMarks}</Layer>

      {/* Anneaux intérieurs fins et anneau central crème. */}
      <Layer size={size} rotate={inner}>
        {[[0.6, [[0.1, 1.9], [2.1, 3.3], [3.5, 6.0]]], [0.52, [[0.6, 2.8], [3.4, 5.6]]]].map(([r, arcs], i) => (
          <G key={i}>
            {(arcs as number[][]).map(([a0, a1], j) => (
              <Path key={j} d={arcPath(c, c, R * (r as number), a0, a1)} stroke={colors.mint} strokeOpacity={0.35}
                strokeWidth={1} fill="none" />
            ))}
          </G>
        ))}
        <Circle cx={c} cy={c} r={R * 0.44} stroke={colors.cream} strokeOpacity={0.55} strokeWidth={1.2} fill="none" />
        <Circle cx={c} cy={c} r={R * 0.415} stroke={colors.gold} strokeOpacity={0.35} strokeWidth={1} fill="none" />
      </Layer>

      {showName ? (
        <View style={[StyleSheet.absoluteFill, styles.center]} pointerEvents="none">
          <Text style={[styles.name, { fontSize: Math.max(11, size * 0.05) }]}>ORION</Text>
        </View>
      ) : null}
    </Animated.View>
  );
}

const styles = StyleSheet.create({
  center: { alignItems: 'center', justifyContent: 'center' },
  name: {
    fontFamily: fonts.display,
    color: colors.cream,
    letterSpacing: 4,
    paddingLeft: 4, // compense l'espacement après la dernière lettre
    textShadowColor: rgba(colors.gold, 0.9),
    textShadowRadius: 10,
  },
});
