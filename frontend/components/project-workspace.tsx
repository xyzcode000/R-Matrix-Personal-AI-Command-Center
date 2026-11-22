"use client";

import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api, type ApplicationTask, type Message, type ProjectSource } from "@/components/api";

type ProjectStatus = "active" | "planned" | "paused" | "completed" | "archived";
type Priority = "low" | "medium" | "high" | "urgent";
type Project = {
  id: string;
  name: string;
  description: string;
  workspace_id: string;
  path: string;
  status: ProjectStatus;
  priority: Priority;
  progress: number;
  start_date: string | null;
  target_date: string | null;
  tech_stack: string[];
  github_url: string | null;
  last_activity_at: string;
  authorization_status: string;
};
type Workspace = {
  id: string;
  name: string;
  path: string;
  indexing_status: string;
  last_indexed_at: string | null;
  search_available: boolean;
};
type ProjectFile = { path: string; name: string; extension: string; size_bytes: number; modified_at: string };
type ProjectFileContent = { path: string; content: string };
type Milestone = {
  id: string;
  title: string;
  description: string;
  target_date: string | null;
  status: "pending" | "in_progress" | "completed";
};
type ProjectNote = {
  id: string;
  title: string;
  content: string;
  category: string;
  updated_at: string;
};
type SearchResult = { path: string; snippet: string };
type Tab = "overview" | "explorer" | "plan" | "knowledge" | "assistant";

const categories = [
  "architecture", "decision", "development", "requirement", "bug", "implementation", "idea",
] as const;

function getError(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

function localDate(value: string | null): string {
  if (!value) return "Not set";
  return new Intl.DateTimeFormat("en-IN", { dateStyle: "medium" }).format(new Date(`${value}T00:00:00`));
}

export function ProjectWorkspace() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [tab, setTab] = useState<Tab>("overview");
  const [tasks, setTasks] = useState<ApplicationTask[]>([]);
  const [milestones, setMilestones] = useState<Milestone[]>([]);
  const [notes, setNotes] = useState<ProjectNote[]>([]);
  const [files, setFiles] = useState<ProjectFile[]>([]);
  const [fileName, setFileName] = useState("");
  const [fileExtension, setFileExtension] = useState("");
  const [fileFolder, setFileFolder] = useState("");
  const [fileContent, setFileContent] = useState<ProjectFileContent | null>(null);
  const [searchText, setSearchText] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<Message | null>(null);
  const [allowCloudProjectContext, setAllowCloudProjectContext] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [newProjectName, setNewProjectName] = useState("");
  const [newProjectDescription, setNewProjectDescription] = useState("");
  const [newProjectWorkspace, setNewProjectWorkspace] = useState("");
  const [taskTitle, setTaskTitle] = useState("");
  const [taskDescription, setTaskDescription] = useState("");
  const [taskPriority, setTaskPriority] = useState<Priority>("medium");
  const [taskMilestone, setTaskMilestone] = useState("");
  const [milestoneTitle, setMilestoneTitle] = useState("");
  const [milestoneDate, setMilestoneDate] = useState("");
  const [noteTitle, setNoteTitle] = useState("");
  const [noteContent, setNoteContent] = useState("");
  const [noteCategory, setNoteCategory] = useState<(typeof categories)[number]>("development");
  const [editingNote, setEditingNote] = useState<string | null>(null);
  const [projectDraft, setProjectDraft] = useState<Project | null>(null);

  const selectedProject = useMemo(
    () => projects.find((project) => project.id === selectedId) ?? null,
    [projects, selectedId],
  );

  const refreshProjects = useCallback(async () => {
    const response = await api<{ projects: Project[]; workspaces: Workspace[] }>("/api/projects");
    setProjects(response.projects);
    setWorkspaces(response.workspaces);
    setSelectedId((current) => response.projects.some((project) => project.id === current)
      ? current
      : response.projects[0]?.id ?? "");
    setLoading(false);
  }, []);

  const refreshProjectData = useCallback(async (projectId: string) => {
    const encoded = encodeURIComponent(projectId);
    const [projectTasks, projectMilestones, projectNotes, projectFiles] = await Promise.all([
      api<ApplicationTask[]>(`/api/tasks?project_id=${encoded}`),
      api<Milestone[]>(`/api/projects/${encoded}/milestones`),
      api<ProjectNote[]>(`/api/projects/${encoded}/notes`),
      api<ProjectFile[]>(`/api/projects/${encoded}/files`),
    ]);
    setTasks(projectTasks);
    setMilestones(projectMilestones);
    setNotes(projectNotes);
    setFiles(projectFiles);
  }, []);

  useEffect(() => {
    void refreshProjects().catch((reason: unknown) => {
      setError(getError(reason, "Could not load projects."));
      setLoading(false);
    });
  }, [refreshProjects]);

  useEffect(() => {
    if (!selectedId) {
      setTasks([]);
      setMilestones([]);
      setNotes([]);
      setFiles([]);
      return;
    }
    setProjectDraft(projects.find((project) => project.id === selectedId) ?? null);
    setFileContent(null);
    setSearchResults([]);
    void refreshProjectData(selectedId).catch((reason: unknown) => {
      setError(getError(reason, "Could not load project workspace data."));
    });
  }, [projects, refreshProjectData, selectedId]);

  async function mutate(action: () => Promise<unknown>, success?: () => void) {
    setBusy(true);
    setError("");
    try {
      await action();
      success?.();
      await refreshProjects();
      if (selectedId) await refreshProjectData(selectedId);
    } catch (reason) {
      setError(getError(reason, "The project update failed."));
    } finally {
      setBusy(false);
    }
  }

  async function createProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!newProjectWorkspace) return;
    await mutate(async () => {
      const created = await api<Project>("/api/projects", {
        method: "POST",
        body: JSON.stringify({
          name: newProjectName,
          description: newProjectDescription,
          workspace_id: newProjectWorkspace,
        }),
      });
      setSelectedId(created.id);
      setNewProjectName("");
      setNewProjectDescription("");
    });
  }

  async function saveProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!projectDraft) return;
    await mutate(() => api(`/api/projects/${encodeURIComponent(projectDraft.id)}`, {
      method: "PUT",
      body: JSON.stringify({
        name: projectDraft.name,
        description: projectDraft.description,
        status: projectDraft.status,
        priority: projectDraft.priority,
        progress: projectDraft.progress,
        start_date: projectDraft.start_date || null,
        target_date: projectDraft.target_date || null,
        tech_stack: projectDraft.tech_stack,
        github_url: projectDraft.github_url || null,
      }),
    }));
  }

  async function deleteProject() {
    if (!selectedProject || !window.confirm(`Delete “${selectedProject.name}”? Projects with linked work must be archived instead.`)) return;
    await mutate(() => api(`/api/projects/${encodeURIComponent(selectedProject.id)}`, { method: "DELETE" }), () => {
      setSelectedId("");
    });
  }

  async function filterFiles(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedProject) return;
    setBusy(true);
    setError("");
    const query = new URLSearchParams({
      name: fileName,
      extension: fileExtension,
      folder: fileFolder,
    });
    try {
      setFiles(await api<ProjectFile[]>(
        `/api/projects/${encodeURIComponent(selectedProject.id)}/files?${query.toString()}`,
      ));
    } catch (reason) {
      setError(getError(reason, "Could not filter project files."));
    } finally {
      setBusy(false);
    }
  }

  async function openFile(path: string) {
    if (!selectedProject) return;
    setBusy(true);
    setError("");
    try {
      setFileContent(await api<ProjectFileContent>(
        `/api/projects/${encodeURIComponent(selectedProject.id)}/files/content?path=${encodeURIComponent(path)}`,
      ));
    } catch (reason) {
      setError(getError(reason, "Could not open this source excerpt."));
    } finally {
      setBusy(false);
    }
  }

  async function searchFiles(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedProject || !searchText.trim()) return;
    setBusy(true);
    setError("");
    try {
      setSearchResults(await api<SearchResult[]>(
        `/api/projects/${encodeURIComponent(selectedProject.id)}/search`,
        { method: "POST", body: JSON.stringify({ query: searchText.trim() }) },
      ));
    } catch (reason) {
      setError(getError(reason, "Project search failed."));
    } finally {
      setBusy(false);
    }
  }

  async function addMilestone(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedProject) return;
    await mutate(() => api(`/api/projects/${encodeURIComponent(selectedProject.id)}/milestones`, {
      method: "POST",
      body: JSON.stringify({ title: milestoneTitle, target_date: milestoneDate || null }),
    }), () => {
      setMilestoneTitle("");
      setMilestoneDate("");
    });
  }

  async function updateMilestone(milestone: Milestone, status: Milestone["status"]) {
    if (!selectedProject) return;
    await mutate(() => api(
      `/api/projects/${encodeURIComponent(selectedProject.id)}/milestones/${encodeURIComponent(milestone.id)}`,
      {
        method: "PUT",
        body: JSON.stringify({
          title: milestone.title,
          description: milestone.description,
          target_date: milestone.target_date,
          status,
        }),
      },
    ));
  }

  async function removeMilestone(milestone: Milestone) {
    if (!selectedProject || !window.confirm(`Delete milestone “${milestone.title}”?`)) return;
    await mutate(() => api(
      `/api/projects/${encodeURIComponent(selectedProject.id)}/milestones/${encodeURIComponent(milestone.id)}`,
      { method: "DELETE" },
    ));
  }

  async function addTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedProject) return;
    await mutate(() => api("/api/tasks", {
      method: "POST",
      body: JSON.stringify({
        title: taskTitle,
        description: taskDescription,
        priority: taskPriority,
        project_id: selectedProject.id,
        milestone_id: taskMilestone || null,
      }),
    }), () => {
      setTaskTitle("");
      setTaskDescription("");
      setTaskMilestone("");
    });
  }

  async function changeTaskStatus(task: ApplicationTask, status: "pending" | "in_progress" | "blocked" | "done") {
    await mutate(() => api(
      `/api/tasks/${encodeURIComponent(task.id)}/${status === "done" ? "complete" : "status"}`,
      {
        method: "POST",
        ...(status === "done" ? {} : { body: JSON.stringify({ status }) }),
      },
    ));
  }

  async function addNote(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedProject) return;
    const path = `/api/projects/${encodeURIComponent(selectedProject.id)}/notes`;
    await mutate(() => api(
      editingNote ? `${path}/${encodeURIComponent(editingNote)}` : path,
      {
        method: editingNote ? "PUT" : "POST",
        body: JSON.stringify({ title: noteTitle, content: noteContent, category: noteCategory }),
      },
    ), () => {
      setNoteTitle("");
      setNoteContent("");
      setEditingNote(null);
    });
  }

  async function removeNote(note: ProjectNote) {
    if (!selectedProject || !window.confirm(`Delete note “${note.title}”?`)) return;
    await mutate(() => api(
      `/api/projects/${encodeURIComponent(selectedProject.id)}/notes/${encodeURIComponent(note.id)}`,
      { method: "DELETE" },
    ));
  }

  async function askAssistant(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedProject || !question.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      const response = await api<{ message: Message }>("/api/chat", {
        method: "POST",
        body: JSON.stringify({
          message: question.trim(),
          project_id: selectedProject.id,
          allow_cloud_project_context: allowCloudProjectContext,
        }),
      });
      setAnswer(response.message);
      setQuestion("");
    } catch (reason) {
      setError(getError(reason, "The project assistant could not answer."));
    } finally {
      setBusy(false);
    }
  }

  const tabs: { id: Tab; label: string }[] = [
    { id: "overview", label: "Overview" },
    { id: "explorer", label: "Codebase" },
    { id: "plan", label: "Milestones & tasks" },
    { id: "knowledge", label: "Project knowledge" },
    { id: "assistant", label: "AI assistant" },
  ];

  return (
    <div className="project-workspace">
      <header className="page-heading">
        <div className="eyebrow">PROJECT INTELLIGENCE · READ-ONLY WORKSPACE ACCESS</div>
        <h1>Projects</h1>
        <p className="subheading">A project workspace for codebase understanding, shared task history, project knowledge, and evidence-grounded AI planning.</p>
      </header>
      {error && <div className="notice" role="alert">{error}</div>}

      <section className="panel project-selector">
        <div className="project-selector__choice">
          <label htmlFor="project-choice">Selected project</label>
          <select
            id="project-choice"
            className="field"
            value={selectedId}
            onChange={(event) => setSelectedId(event.target.value)}
            disabled={loading || projects.length === 0}
          >
            {projects.map((project) => (
              <option key={project.id} value={project.id}>{project.name} · {project.status}</option>
            ))}
          </select>
        </div>
        <details className="project-create">
          <summary className="button button-secondary">Create project</summary>
          <form onSubmit={(event) => void createProject(event)} className="project-form">
            <label>Project name<input className="field" required maxLength={120} value={newProjectName} onChange={(event) => setNewProjectName(event.target.value)} /></label>
            <label>Authorized workspace
              <select className="field" value={newProjectWorkspace} required onChange={(event) => setNewProjectWorkspace(event.target.value)}>
                <option value="">Select an authorized folder</option>
                {workspaces.map((workspace) => <option key={workspace.id} value={workspace.id}>{workspace.name} · {workspace.path}</option>)}
              </select>
            </label>
            <label>Description<textarea className="field" maxLength={2000} value={newProjectDescription} onChange={(event) => setNewProjectDescription(event.target.value)} /></label>
            <button className="button" disabled={busy}>Create project</button>
          </form>
        </details>
      </section>

      {!loading && projects.length === 0 && (
        <div className="empty-state">No configured, authorized workspaces are available. Configure AUTHORIZED_PROJECT_DIRS in backend/.env to expose a folder to read-only project search.</div>
      )}

      {selectedProject && projectDraft && (
        <>
          <nav className="project-tabs" aria-label="Project workspace">
            {tabs.map((item) => (
              <button
                type="button"
                key={item.id}
                className={tab === item.id ? "is-active" : ""}
                aria-pressed={tab === item.id}
                onClick={() => setTab(item.id)}
              >
                {item.label}
              </button>
            ))}
          </nav>

          {tab === "overview" && (
            <div className="project-overview">
              <section className="panel">
                <div className="panel-title"><h2>Project overview</h2><span className="quiet-label">{selectedProject.authorization_status}</span></div>
                <form className="project-form" onSubmit={(event) => void saveProject(event)}>
                  <label>Name<input className="field" required maxLength={120} value={projectDraft.name} onChange={(event) => setProjectDraft({ ...projectDraft, name: event.target.value })} /></label>
                  <label>Description<textarea className="field" maxLength={2000} value={projectDraft.description} onChange={(event) => setProjectDraft({ ...projectDraft, description: event.target.value })} /></label>
                  <label>Status
                    <select className="field" value={projectDraft.status} onChange={(event) => setProjectDraft({ ...projectDraft, status: event.target.value as ProjectStatus })}>
                      {["active", "planned", "paused", "completed", "archived"].map((status) => <option key={status} value={status}>{status}</option>)}
                    </select>
                  </label>
                  <label>Priority
                    <select className="field" value={projectDraft.priority} onChange={(event) => setProjectDraft({ ...projectDraft, priority: event.target.value as Priority })}>
                      {["low", "medium", "high", "urgent"].map((priority) => <option key={priority} value={priority}>{priority}</option>)}
                    </select>
                  </label>
                  <label>Progress · {projectDraft.progress}%
                    <input className="field" type="range" min="0" max="100" value={projectDraft.progress} onChange={(event) => setProjectDraft({ ...projectDraft, progress: Number(event.target.value) })} />
                  </label>
                  <label>Start date<input className="field" type="date" value={projectDraft.start_date ?? ""} onChange={(event) => setProjectDraft({ ...projectDraft, start_date: event.target.value || null })} /></label>
                  <label>Target completion<input className="field" type="date" value={projectDraft.target_date ?? ""} onChange={(event) => setProjectDraft({ ...projectDraft, target_date: event.target.value || null })} /></label>
                  <label>Technology stack<input className="field" value={projectDraft.tech_stack.join(", ")} onChange={(event) => setProjectDraft({ ...projectDraft, tech_stack: event.target.value.split(",").map((part) => part.trim()).filter(Boolean).slice(0, 20) })} placeholder="Next.js, FastAPI, SQLite" /></label>
                  <label>GitHub repository<input className="field" type="url" value={projectDraft.github_url ?? ""} onChange={(event) => setProjectDraft({ ...projectDraft, github_url: event.target.value || null })} placeholder="https://github.com/owner/repository" /></label>
                  <button className="button" disabled={busy}>Save project</button>
                </form>
              </section>
              <section className="panel">
                <div className="panel-title"><h2>Authorized workspace</h2><span className="quiet-label">Read-only · on-demand scan</span></div>
                <p className="project-path"><strong>{selectedProject.path}</strong></p>
                <p>Search availability: {selectedProject.authorization_status === "authorized" ? "Available" : "Unavailable"}. Files are never modified or executed.</p>
                <p>Last activity: {new Date(selectedProject.last_activity_at).toLocaleString()}</p>
                <p>Start: {localDate(selectedProject.start_date)} · Target: {localDate(selectedProject.target_date)}</p>
                {selectedProject.github_url && <a href={selectedProject.github_url} target="_blank" rel="noreferrer">Open configured repository ↗</a>}
                <div className="project-overview__actions">
                  <button className="button button-danger" disabled={busy} onClick={() => void deleteProject()}>Delete project</button>
                  <span className="quiet-label">Linked history is preserved; archive projects that contain tasks or knowledge.</span>
                </div>
              </section>
              <section className="panel">
                <div className="panel-title"><h2>Configured workspace roots</h2><span className="quiet-label">{workspaces.length} authorized</span></div>
                {workspaces.map((workspace) => (
                  <div className="list-row" key={workspace.id}>
                    <div className="list-copy"><strong>{workspace.name}</strong><small>{workspace.path} · {workspace.indexing_status} · Not indexed or cached</small></div>
                    <span className={workspace.search_available ? "configured-label" : "unconfigured-label"}>{workspace.search_available ? "Authorized" : "Unavailable"}</span>
                  </div>
                ))}
              </section>
            </div>
          )}

          {tab === "explorer" && (
            <section className="panel">
              <div className="panel-title"><h2>Codebase explorer</h2><span className="quiet-label">Bounded text files · max 80 listed · no execution</span></div>
              <form className="project-filter" onSubmit={(event) => void filterFiles(event)}>
                <input className="field" aria-label="Filter by file name" placeholder="File name" value={fileName} onChange={(event) => setFileName(event.target.value)} />
                <input className="field" aria-label="Filter by extension" placeholder="Extension · e.g. .tsx" value={fileExtension} onChange={(event) => setFileExtension(event.target.value)} />
                <input className="field" aria-label="Filter by folder" placeholder="Folder path" value={fileFolder} onChange={(event) => setFileFolder(event.target.value)} />
                <button className="button button-secondary" disabled={busy}>Filter files</button>
              </form>
              <div className="project-file-list">
                {files.length ? files.map((file) => (
                  <button type="button" key={file.path} className="project-file" onClick={() => void openFile(file.path)}>
                    <span><strong>{file.path}</strong><small>{file.extension || "text"} · {file.size_bytes.toLocaleString()} bytes · {new Date(file.modified_at).toLocaleDateString()}</small></span>
                    <span aria-hidden="true">↗</span>
                  </button>
                )) : <div className="empty-state">No supported text files match these filters.</div>}
              </div>
              {fileContent && <article className="project-source"><div className="panel-title"><h3>{fileContent.path}</h3><button className="button button-secondary" onClick={() => setFileContent(null)}>Close excerpt</button></div><pre>{fileContent.content}</pre></article>}
              <form className="project-filter project-search" onSubmit={(event) => void searchFiles(event)}>
                <input className="field" aria-label="Search source content" placeholder="Search bounded source content" value={searchText} onChange={(event) => setSearchText(event.target.value)} />
                <button className="button" disabled={busy || !searchText.trim()}>Search code</button>
              </form>
              {searchResults.map((result) => <article className="search-result" key={`${result.path}-${result.snippet.slice(0, 30)}`}><strong>{result.path}</strong><p>{result.snippet}</p></article>)}
            </section>
          )}

          {tab === "plan" && (
            <div className="project-overview">
              <section className="panel">
                <div className="panel-title"><h2>Milestones</h2><span className="quiet-label">{milestones.length} saved</span></div>
                <form className="project-filter" onSubmit={(event) => void addMilestone(event)}>
                  <input className="field" required maxLength={160} placeholder="Milestone title" value={milestoneTitle} onChange={(event) => setMilestoneTitle(event.target.value)} />
                  <input className="field" type="date" aria-label="Milestone target date" value={milestoneDate} onChange={(event) => setMilestoneDate(event.target.value)} />
                  <button className="button" disabled={busy}>Add milestone</button>
                </form>
                {milestones.map((milestone) => <div className="list-row" key={milestone.id}>
                  <div className="list-copy"><strong>{milestone.title}</strong><small>{milestone.status} · target {localDate(milestone.target_date)}</small></div>
                  <select className="field project-status-select" aria-label={`Status for ${milestone.title}`} value={milestone.status} onChange={(event) => void updateMilestone(milestone, event.target.value as Milestone["status"])}>
                    {["pending", "in_progress", "completed"].map((status) => <option key={status} value={status}>{status}</option>)}
                  </select>
                  <button className="button button-danger" onClick={() => void removeMilestone(milestone)}>Delete</button>
                </div>)}
              </section>
              <section className="panel">
                <div className="panel-title"><h2>Project tasks</h2><span className="quiet-label">Shared with Task History</span></div>
                <form className="project-form" onSubmit={(event) => void addTask(event)}>
                  <label>Task<input className="field" required maxLength={180} value={taskTitle} onChange={(event) => setTaskTitle(event.target.value)} placeholder="Next verified step" /></label>
                  <label>Description<textarea className="field" maxLength={1000} value={taskDescription} onChange={(event) => setTaskDescription(event.target.value)} /></label>
                  <label>Priority<select className="field" value={taskPriority} onChange={(event) => setTaskPriority(event.target.value as Priority)}>{["low", "medium", "high", "urgent"].map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
                  <label>Milestone<select className="field" value={taskMilestone} onChange={(event) => setTaskMilestone(event.target.value)}><option value="">No milestone</option>{milestones.map((milestone) => <option key={milestone.id} value={milestone.id}>{milestone.title}</option>)}</select></label>
                  <button className="button" disabled={busy}>Add to Task History</button>
                </form>
                {tasks.length ? tasks.map((task) => <div className="list-row" key={task.id}>
                  <div className="list-copy"><strong>{task.title}</strong><small>{task.status} · {task.priority}{task.due_at ? ` · due ${new Date(task.due_at).toLocaleString()}` : ""}{task.milestone_id ? ` · ${milestones.find((item) => item.id === task.milestone_id)?.title ?? "Milestone"}` : ""}</small>{task.description && <small>{task.description}</small>}</div>
                  {task.status !== "done" && <select className="field project-status-select" aria-label={`Status for ${task.title}`} value={task.status} onChange={(event) => void changeTaskStatus(task, event.target.value as "pending" | "in_progress" | "blocked" | "done")}><option value="pending">pending</option><option value="in_progress">in progress</option><option value="blocked">blocked</option><option value="done">done</option></select>}
                </div>) : <div className="empty-state">No tasks are linked to this project. New project tasks will also appear in Task History.</div>}
              </section>
            </div>
          )}

          {tab === "knowledge" && (
            <section className="panel">
              <div className="panel-title"><h2>Project knowledge</h2><span className="quiet-label">Scoped to {selectedProject.name}</span></div>
              <form className="project-form" onSubmit={(event) => void addNote(event)}>
                <label>Title<input className="field" required maxLength={160} value={noteTitle} onChange={(event) => setNoteTitle(event.target.value)} /></label>
                <label>Category<select className="field" value={noteCategory} onChange={(event) => setNoteCategory(event.target.value as (typeof categories)[number])}>{categories.map((category) => <option key={category} value={category}>{category}</option>)}</select></label>
                <label>Knowledge<textarea className="field" required maxLength={5000} value={noteContent} onChange={(event) => setNoteContent(event.target.value)} placeholder="Architecture notes, technical decisions, requirements, bugs, or next ideas" /></label>
                <div className="project-form__actions"><button className="button" disabled={busy}>{editingNote ? "Save changes" : "Save project knowledge"}</button>{editingNote && <button className="button button-secondary" type="button" onClick={() => { setEditingNote(null); setNoteTitle(""); setNoteContent(""); }}>Cancel edit</button>}</div>
              </form>
              {notes.length ? notes.map((note) => <article className="project-note" key={note.id}>
                <div className="panel-title"><div><span className="eyebrow">{note.category}</span><h3>{note.title}</h3></div><small>{new Date(note.updated_at).toLocaleString()}</small></div>
                <p>{note.content}</p>
                <div className="project-form__actions">
                  <button className="button button-secondary" onClick={() => { setEditingNote(note.id); setNoteTitle(note.title); setNoteContent(note.content); setNoteCategory(note.category as (typeof categories)[number]); }}>Edit</button>
                  <button className="button button-danger" onClick={() => void removeNote(note)}>Delete</button>
                </div>
              </article>) : <div className="empty-state">No project notes yet. Add verified implementation details or decisions; the project assistant will retrieve these only for this project.</div>}
            </section>
          )}

          {tab === "assistant" && (
            <section className="panel project-assistant">
              <div className="panel-title"><h2>Project Intelligence</h2><span className="quiet-label">{selectedProject.name} · bounded context</span></div>
              <p>Ask about architecture, code locations, progress, milestones, or next steps. Answers use this project’s approved notes, shared tasks, milestones, and bounded source excerpts; code paths are cited when available.</p>
              <label className="project-consent"><input type="checkbox" checked={allowCloudProjectContext} onChange={(event) => setAllowCloudProjectContext(event.target.checked)} /> Allow the selected cloud provider to receive this project’s bounded excerpts and project context for this request.</label>
              <form className="project-filter" onSubmit={(event) => void askAssistant(event)}>
                <textarea className="field" required maxLength={8000} value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="What should I work on next in this project?" />
                <button className="button" disabled={busy || !question.trim()}>{busy ? "Working…" : "Ask project assistant"}</button>
              </form>
              {answer && <article className="project-answer"><div className="eyebrow">PROJECT RESPONSE · {answer.execution?.status ?? "recorded"}</div><p>{answer.content}</p>{answer.sources.filter((source): source is ProjectSource => source.kind === "project_file").map((source, index) => <small key={`${source.document_id ?? source.filename ?? source.path}-${index}`}>Source: {String(source.path)}</small>)}</article>}
            </section>
          )}
        </>
      )}
    </div>
  );
}
