const logBox = document.getElementById('job-log');
if (logBox) {
  const url = logBox.dataset.url;
  const refresh = async () => {
    const res = await fetch(url);
    const data = await res.json();
    logBox.textContent = data.logs.join('\n');
    if (data.status === 'RUNNING' || data.status === 'PENDING') setTimeout(refresh, 2000);
  };
  setTimeout(refresh, 2000);
}
