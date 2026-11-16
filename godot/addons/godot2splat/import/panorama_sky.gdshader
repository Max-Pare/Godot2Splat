// Shows the sky panorama baked by the g2s capture.
// Layout (see src/g2s/geometry.py, panorama_directions): the image centre looks
// along -Z, u grows towards +X, the top row is straight up.
shader_type sky;

uniform sampler2D panorama : source_color, filter_linear, repeat_enable;

void sky() {
	vec3 d = normalize(EYEDIR);
	vec2 uv = vec2(atan(d.x, -d.z) / TAU + 0.5, acos(clamp(d.y, -1.0, 1.0)) / PI);
	COLOR = texture(panorama, uv).rgb;
}
