// Smooth noise gate (downward expander) that runs after RNNoise.
// RNNoise removes most noise but can leave soft hiss when nobody is talking. This learns the background level by
// itself and turns the sound down by 18 dB while you're quiet, fading in and out so it never clicks or cuts words.
const db = (x) => 20 * Math.log10(x + 1e-9);
class SoftGate extends AudioWorkletProcessor {
  constructor() {
    super();
    const block = 128 / sampleRate;
    this.env = -90;                                                  // smoothed level, dBFS
    this.hist = new Float32Array(Math.round(3 / block)).fill(-90);   // last 3 s of levels
    this.pos = 0;
    this.filled = 0;
    this.floor = -90;                                                // background estimate
    this.open = true;                                                // start open: never eat the first words
    this.hold = 0;
    this.gain = 1;
    this.holdBlocks = Math.round(0.3 / block);                       // stay open 300 ms after speech
    this.atk = 1 - Math.exp(-1 / (0.005 * sampleRate));              // open in ~5 ms
    this.rel = 1 - Math.exp(-1 / (0.15 * sampleRate));               // close over ~150 ms
    this.low = 10 ** (-18 / 20);
    this.tick = 0;
  }
  process(inputs, outputs) {
    const inp = inputs[0], out = outputs[0];
    if (!inp || !inp[0]) return true;
    const x = inp[0];
    let sum = 0;
    for (let i = 0; i < x.length; i++) sum += x[i] * x[i];
    const level = db(Math.sqrt(sum / x.length));
    // smoothed level: rises in ~10 ms (a word opens it quickly), falls in ~30 ms; single spikes don't count
    this.env += (level - this.env) * (level > this.env ? 0.25 : 0.08);
    if (level > -90) { // digital silence (muted mic, start of the stream) isn't background noise
      this.hist[this.pos] = this.env;
      this.pos = (this.pos + 1) % this.hist.length;
      this.filled = Math.min(this.hist.length, this.filled + 1);
    }
    // background = the quietest 20% of the last 3 s. Speech always has small gaps, so talking doesn't raise it;
    // when nobody talks it settles on the hiss level.
    if (++this.tick % 32 === 0 && this.filled > this.hist.length / 3) {
      const s = this.hist.slice(0, this.filled).sort();
      this.floor = s[Math.floor(s.length * 0.2)];
    }
    const learning = this.filled <= this.hist.length / 3; // first second: just listen
    const openAt = Math.min(-30, Math.max(-55, this.floor + 6));
    if (learning || this.env > openAt) { this.open = true; this.hold = this.holdBlocks; }
    else if (this.env < openAt - 3 && this.open && --this.hold <= 0) this.open = false;
    const target = this.open ? 1 : this.low;
    for (let c = 0; c < out.length; c++) {
      const src = inp[c] || x, dst = out[c];
      let g = this.gain;
      for (let i = 0; i < dst.length; i++) {
        g += (target - g) * (target > g ? this.atk : this.rel);
        dst[i] = src[i] * g;
      }
      if (c === out.length - 1) this.gain = g;
    }
    return true;
  }
}
registerProcessor('vc-soft-gate', SoftGate);
