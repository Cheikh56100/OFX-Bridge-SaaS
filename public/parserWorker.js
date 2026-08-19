let pyodidePromise = null;

async function init() {
  if (pyodidePromise) return pyodidePromise;
  importScripts('https://cdn.jsdelivr.net/pyodide/v0.27.2/full/pyodide.js');
  pyodidePromise = (async () => {
    const pyodide = await loadPyodide();
    const source = await fetch('/engine.py').then(r => r.text());
    await pyodide.runPythonAsync(source);
    return pyodide;
  })();
  return pyodidePromise;
}

self.onmessage = async (event) => {
  try {
    const pyodide = await init();
    const input = JSON.stringify(event.data);
    const output = pyodide.runPython(`run_json(${JSON.stringify(input)})`);
    const parsed = JSON.parse(output);
    self.postMessage({ ...parsed, __id: event.data.__id });
  } catch (error) {
    self.postMessage({ error: error?.message || String(error), __id: event.data?.__id });
  }
};
