/** Demand-rendered WebGL2 decoration. Samples a synthetic light field, never DOM/text. */
export function glassRenderer(canvas: HTMLCanvasElement, dark: boolean) {
  const gl = canvas.getContext('webgl2', { alpha: true, antialias: false, powerPreference: 'low-power', premultipliedAlpha: false });
  if (!gl) return () => {};
  const vertex = `#version 300 es
  in vec2 position; out vec2 uv;
  void main(){uv=position*.5+.5;gl_Position=vec4(position,0.,1.);}`;
  const fragment = `#version 300 es
  precision highp float;
  in vec2 uv; out vec4 color;
  uniform vec2 pointer; uniform float darkMode;
  float roundBox(vec2 p,vec2 b,float r){vec2 q=abs(p)-b+r;return min(max(q.x,q.y),0.)+length(max(q,0.))-r;}
  void main(){
    vec2 p=uv; vec2 drift=(pointer-.5)*.035;
    float w=sin((p.x+drift.x)*5.7+p.y*2.)*.10+.40;
    float band=exp(-pow((p.y-w)/.16,2.));
    float line=exp(-pow((p.y-w-.08)/.0035,2.));
    float glow=exp(-length((p-vec2(.72,.68)-drift)*vec2(1.4,2.))*4.);
    float d=roundBox((p-.5)*vec2(2.,1.),vec2(.98,.48),.13);
    float edge=exp(-abs(d)*240.);
    vec3 blue=mix(vec3(.43,.72,1.),vec3(.16,.38,.79),darkMode);
    vec3 lilac=mix(vec3(.78,.65,1.),vec3(.4,.25,.64),darkMode);
    vec3 tint=mix(blue,lilac,p.x*.9);
    vec3 light=tint*(.48+band*.42)+vec3(line*.32+edge*.5+glow*.15);
    float alpha=.12+band*.12+glow*.12+edge*.2+line*.16;
    color=vec4(light,alpha);
  }`;
  function compile(type: number, source: string) {
    const shader = gl!.createShader(type)!;
    gl!.shaderSource(shader, source); gl!.compileShader(shader);
    if (!gl!.getShaderParameter(shader, gl!.COMPILE_STATUS)) { gl!.deleteShader(shader); return null; }
    return shader;
  }
  const vs = compile(gl.VERTEX_SHADER, vertex), fs = compile(gl.FRAGMENT_SHADER, fragment);
  if (!vs || !fs) { if(vs)gl.deleteShader(vs); if(fs)gl.deleteShader(fs); return () => {}; }
  const program = gl.createProgram()!;
  gl.attachShader(program, vs); gl.attachShader(program, fs); gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) { gl.deleteProgram(program); gl.deleteShader(vs); gl.deleteShader(fs); return () => {}; }
  const buffer = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1]), gl.STATIC_DRAW);
  gl.useProgram(program);
  const pos = gl.getAttribLocation(program, 'position');
  gl.enableVertexAttribArray(pos); gl.vertexAttribPointer(pos, 2, gl.FLOAT, false, 0, 0);
  const pointer = gl.getUniformLocation(program, 'pointer');
  gl.uniform1f(gl.getUniformLocation(program, 'darkMode'), dark ? 1 : 0);
  let x = .5, y = .5, frame = 0, disposed = false;
  const paint = () => {
    frame = 0;
    if (disposed || document.hidden || gl.isContextLost()) return;
    const ratio = Math.min(devicePixelRatio || 1, 1.75);
    const w = Math.round(canvas.clientWidth * ratio), h = Math.round(canvas.clientHeight * ratio);
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    gl.viewport(0, 0, w, h); gl.uniform2f(pointer, x, y); gl.drawArrays(gl.TRIANGLES, 0, 6);
  };
  const schedule = () => { if (!frame && !disposed) frame = requestAnimationFrame(paint); };
  const move = (event: PointerEvent) => {
    const rect = canvas.getBoundingClientRect();
    x = (event.clientX - rect.left) / rect.width; y = 1 - (event.clientY - rect.top) / rect.height;
    schedule();
  };
  const parent = canvas.parentElement!;
  parent.addEventListener('pointermove', move);
  const observer = new ResizeObserver(schedule); observer.observe(canvas);
  document.addEventListener('visibilitychange', schedule);
  const lost = (event: Event) => { event.preventDefault(); canvas.style.opacity = '0'; };
  const restored = () => { canvas.style.opacity = '0'; }; // CSS material remains readable after a GPU reset.
  canvas.addEventListener('webglcontextlost', lost); canvas.addEventListener('webglcontextrestored', restored);
  schedule();
  return () => {
    disposed = true; cancelAnimationFrame(frame); observer.disconnect();
    parent.removeEventListener('pointermove', move); document.removeEventListener('visibilitychange', schedule);
    canvas.removeEventListener('webglcontextlost', lost); canvas.removeEventListener('webglcontextrestored', restored);
    gl.deleteBuffer(buffer); gl.deleteProgram(program); gl.deleteShader(vs); gl.deleteShader(fs);
    gl.getExtension('WEBGL_lose_context')?.loseContext();
  };
}
