// Two-colour 3D checker in world space: gives the test room surfaces something to
// reconstruct without any texture files.
shader_type spatial;

uniform vec3 color_a : source_color = vec3(0.8);
uniform vec3 color_b : source_color = vec3(0.6);
uniform float scale = 4.0;

varying vec3 world_position;

void vertex() {
	world_position = (MODEL_MATRIX * vec4(VERTEX, 1.0)).xyz;
}

void fragment() {
	// The small offset keeps surfaces that lie exactly on a cell border in one cell.
	vec3 cell = floor(world_position * scale + 0.013);
	ALBEDO = mix(color_a, color_b, mod(cell.x + cell.y + cell.z, 2.0));
	ROUGHNESS = 0.8;
}
