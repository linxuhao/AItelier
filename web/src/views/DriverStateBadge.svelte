<script lang="ts">
  import { st } from '../lib/stateI18n.svelte';
  import { driverState, driverStateLabelKey } from '../lib/driverList';
  /** A driver's state: a small CSS mark that moves while the state is live,
   *  then the label. Each state has its own shape and colour, so with motion
   *  reduced (prefers-reduced-motion) the still marks stay distinct. */
  let { phase, title }: { phase?: string | null; title?: string } = $props();
  const label = $derived(st(driverStateLabelKey(phase)));
</script>
<span class="driver-state" data-phase={driverState(phase)} title={title ?? label}><span class="driver-state-mark" aria-hidden="true"></span>{label}</span>
<style>
  .driver-state{display:inline-flex;align-items:center;gap:6px;color:var(--pico-muted-color);}
  .driver-state-mark{position:relative;flex:none;width:14px;height:14px;display:inline-flex;align-items:center;justify-content:center;
    --busy:light-dark(#15803d,#4ade80);--ready:light-dark(#2563eb,#60a5fa);--dormant:light-dark(#7c3aed,#a78bfa);
    --change:light-dark(#b45309,#fbbf24);--failed:light-dark(#b91c1c,#f87171);--stopped:light-dark(#94a3b8,#64748b);}
  .driver-state-mark::before,.driver-state-mark::after{content:'';position:absolute;box-sizing:border-box;}
  /* busy: three dots hopping in turn - at work. Each dot is a box-shadow of a box
     placed just left of the mark (an outer shadow is never painted under its box). */
  [data-phase=busy] .driver-state-mark::before{left:-3px;width:3px;height:3px;border-radius:50%;
    box-shadow:4px 0 0 0 var(--busy),8px 0 0 0 var(--busy),12px 0 0 0 var(--busy);animation:ds-work 1.1s ease-in-out infinite;}
  /* ready: a check in a ring that glows softly - standing by for input */
  [data-phase=ready] .driver-state-mark::before{width:12px;height:12px;border-radius:50%;border:1.5px solid var(--ready);animation:ds-glow 2.6s ease-in-out infinite;}
  [data-phase=ready] .driver-state-mark::after{width:3px;height:6px;margin-top:-1px;border:solid var(--ready);border-width:0 1.5px 1.5px 0;transform:rotate(45deg);}
  /* dormant: a crescent, a small z drifting up */
  [data-phase=dormant] .driver-state-mark::before{width:10px;height:10px;left:0;top:3px;border-radius:50%;box-shadow:3px -2px 0 0 var(--dormant);}
  [data-phase=dormant] .driver-state-mark::after{content:'z';right:-3px;top:-2px;font-size:9px;line-height:1;color:var(--dormant);animation:ds-snore 3s ease-in-out infinite;}
  /* starting: a turning ring */
  [data-phase=starting] .driver-state-mark::before{width:11px;height:11px;border-radius:50%;border:2px solid var(--change);border-top-color:transparent;animation:ds-spin .9s linear infinite;}
  /* stopping: a square winding down */
  [data-phase=stopping] .driver-state-mark::before{width:9px;height:9px;border-radius:2px;background:var(--change);animation:ds-shrink 1.4s ease-in-out infinite;}
  /* failed: a dot blinking twice, then a pause */
  [data-phase=failed] .driver-state-mark::before{width:8px;height:8px;border-radius:50%;background:var(--failed);animation:ds-blink 2s steps(1) infinite;}
  /* stopped: a still square */
  [data-phase=stopped] .driver-state-mark::before{width:8px;height:8px;border-radius:2px;background:var(--stopped);}
  @keyframes ds-work{
    0%,80%,100%{box-shadow:4px 0 0 0 var(--busy),8px 0 0 0 var(--busy),12px 0 0 0 var(--busy)}
    20%{box-shadow:4px -3px 0 0 var(--busy),8px 0 0 0 var(--busy),12px 0 0 0 var(--busy)}
    40%{box-shadow:4px 0 0 0 var(--busy),8px -3px 0 0 var(--busy),12px 0 0 0 var(--busy)}
    60%{box-shadow:4px 0 0 0 var(--busy),8px 0 0 0 var(--busy),12px -3px 0 0 var(--busy)}}
  @keyframes ds-glow{0%,100%{opacity:1}50%{opacity:.4}}
  @keyframes ds-snore{0%{transform:translate(0,3px);opacity:0}40%{opacity:1}100%{transform:translate(3px,-5px);opacity:0}}
  @keyframes ds-spin{to{transform:rotate(360deg)}}
  @keyframes ds-shrink{0%,100%{transform:scale(1)}50%{transform:scale(.5)}}
  @keyframes ds-blink{0%,60%{opacity:1}70%{opacity:.15}80%{opacity:1}90%{opacity:.15}}
  @media (prefers-reduced-motion: reduce){.driver-state-mark::before,.driver-state-mark::after{animation:none;}}
</style>
