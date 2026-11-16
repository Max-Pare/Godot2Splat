// Full-screen pass that writes the scene's depth as colour, for the capture.
//
// Output, read back from a half-float viewport (see capture_runner.gd):
//   depth in whole millimetres mm, split so that each part is exact in a half float:
//   R = floor(mm / 2048) / 2048,  G = (mm mod 2048) / 2048,  B = metres / far
// All three stay within 0..1. B repeats the depth at lower precision as a check.
// 0 everywhere = no surface (sky). Depth is the distance along the camera's
// forward axis. Forward+ and Mobile renderers (reversed depth buffer, 0 = far).
shader_type spatial;
render_mode unshaded, fog_disabled, depth_test_disabled, depth_draw_never, cull_disabled, shadows_disabled;

uniform sampler2D depth_texture : hint_depth_texture, filter_nearest, repeat_disable;
uniform float far = 500.0;

void vertex() {
	// Cover the screen whatever the camera and wherever the mesh is.
	POSITION = vec4(VERTEX.xy, 1.0, 1.0);
}

void fragment() {
	float raw = texture(depth_texture, SCREEN_UV).x;
	vec4 view = INV_PROJECTION_MATRIX * vec4(SCREEN_UV * 2.0 - 1.0, raw, 1.0);
	float metres = -view.z / view.w;
	float mm = raw <= 0.0 ? 0.0 : floor(metres * 1000.0 + 0.5);
	float high = floor(mm / 2048.0);
	ALBEDO = vec3(high / 2048.0, (mm - high * 2048.0) / 2048.0, mm / (far * 1000.0));
}
