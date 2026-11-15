// HandoffBeforePost: turns Remix's output back into the game's linear HDR scene colour.
// Remix writes saturate(pow(L * 2^exposureBias, 1 / 2.2)) (tonemapping_apply_tonemapping.comp.slang, tone curve off,
// dithering off), so L = pow(v, 2.2) * 2^-exposureBias. c0.x = 2.2, c0.y = 2^-exposureBias.
// Build: fxc /T ps_3_0 /E main /O3 /Vn g_rtDecodePs /Fh rt_decode_ps.h rt_decode_ps.hlsl
sampler2D sRemixImage : register(s0);
float4 cDecode : register(c0);

float4 main(float2 uv : TEXCOORD0) : COLOR0 {
    const float3 v = tex2D(sRemixImage, uv).rgb;
    return float4(pow(max(v, 0.0), cDecode.x) * cDecode.y, 1.0);
}
