//!HOOK MAIN
//!BIND HOOKED
//!DESC mediasearch Weissabgleich warm-fix (mittel)
// Feste, luminanz-erhaltende Kanal-Verstaerkung gegen Gelbstich:
// Rot runter, Blau hoch. Laeuft auf der GPU -> auch 4K bleibt fluessig.
vec4 hook() {
    vec4 c = HOOKED_tex(HOOKED_pos);
    vec3 gain = vec3(0.90, 1.00, 1.16);
    gain /= dot(gain, vec3(0.299, 0.587, 0.114));  // Helligkeit konstant halten
    c.rgb = clamp(c.rgb * gain, 0.0, 1.0);
    return c;
}
