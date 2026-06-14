// Server-Sent Events stream of batch progress (image_saved / entry_done / job_*).
export function connectEvents(onEvent) {
  const es = new EventSource('/api/events');
  es.onmessage = (e) => {
    if (!e.data) return;
    try {
      onEvent(JSON.parse(e.data));
    } catch {
      /* ignore malformed frame */
    }
  };
  // EventSource auto-reconnects on error; nothing to do here.
  return es;
}
