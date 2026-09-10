const state = document.querySelector("#state");
const message = document.querySelector("#message");
const project = document.querySelector("#project");
const task = document.querySelector("#task");
const note = document.querySelector("#note");
const start = document.querySelector("#start");
const pause = document.querySelector("#pause");
const stop = document.querySelector("#stop");
let snapshot = null;

async function connection() {
  return chrome.storage.local.get(["bridgeToken", "bridgePort"]);
}

async function request(path, options = {}) {
  const {bridgeToken = "", bridgePort = 8765} = await connection();
  if (!bridgeToken) throw new Error("Open Connection settings and save the token from agent.toml.");
  const response = await fetch(`http://127.0.0.1:${bridgePort}${path}`, {
    ...options,
    headers: {
      "Authorization": `Bearer ${bridgeToken}`,
      "Content-Type": "application/json",
      ...(options.headers || {})
    }
  });
  if (!response.ok) throw new Error(`Desktop tracker rejected the request (${response.status}).`);
  return response.json();
}

function replaceOptions(select, values, selected, emptyLabel) {
  select.replaceChildren();
  if (emptyLabel) {
    const empty = document.createElement("option");
    empty.value = ""; empty.textContent = emptyLabel; select.append(empty);
  }
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value.id; option.textContent = value.name;
    option.selected = value.id === selected; select.append(option);
  }
}

function render(value) {
  const chosenProject = value.state === "stopped" && project.value ? project.value : value.project_id;
  const chosenTask = value.state === "stopped" && task.value ? task.value : value.task_id;
  snapshot = value;
  state.textContent = `${value.state === "stopped" ? "Not tracking" : value.state} · ${value.status}`;
  replaceOptions(project, value.projects, chosenProject, "Choose a project");
  const selectedProject = value.projects.find(item => item.id === project.value);
  replaceOptions(task, selectedProject?.tasks || [], chosenTask, "General project time");
  start.disabled = value.state !== "stopped" || !value.projects.length;
  pause.disabled = value.state === "stopped";
  pause.textContent = value.state === "paused" ? "Resume" : "Pause";
  stop.disabled = value.state === "stopped";
}

async function refresh() {
  try { render(await request("/v1/timer")); message.textContent = ""; }
  catch (error) { message.textContent = error.message; }
}

project.addEventListener("change", () => {
  const selected = snapshot?.projects.find(item => item.id === project.value);
  replaceOptions(task, selected?.tasks || [], "", "General project time");
});

async function act(action) {
  message.textContent = "Saving…";
  try {
    render(await request("/v1/timer", {
      method: "POST",
      body: JSON.stringify({action, project_id: project.value, task_id: task.value, note: note.value})
    }));
    message.textContent = "";
  } catch (error) { message.textContent = error.message; }
}

start.addEventListener("click", () => act("start"));
pause.addEventListener("click", () => act(snapshot?.state === "paused" ? "resume" : "pause"));
stop.addEventListener("click", () => act("stop"));
document.querySelector("#settings").addEventListener("click", event => {
  event.preventDefault(); chrome.runtime.openOptionsPage();
});
refresh();
setInterval(refresh, 2000);
