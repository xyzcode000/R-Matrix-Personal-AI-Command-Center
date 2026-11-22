"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api, type ApplicationTask } from "../../components/api";

type ReminderPreference = ApplicationTask["reminder_preference"];
type TaskView = "active" | "completed" | "all";

function inIndiaDateTime(value: string | null): string {
  if (!value) return "";
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Kolkata",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(value));
  const fields = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${fields.year}-${fields.month}-${fields.day}T${fields.hour}:${fields.minute}`;
}

function utcDue(value: string): string | null {
  if (!value) return null;
  return new Date(`${value}:00+05:30`).toISOString();
}

function deadlineLabel(task: ApplicationTask): string {
  if (!task.due_at) return "No deadline";
  const due = new Date(task.due_at).getTime();
  const now = Date.now();
  if (task.status !== "done" && due < now) return "Overdue";
  if (task.status === "done") return "Completed";
  if (due - now < 24 * 60 * 60 * 1000) return "Due soon";
  return "Upcoming";
}

function deadlineText(value: string | null): string {
  if (!value) return "No deadline";
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export default function TaskHistoryPage() {
  const [tasks, setTasks] = useState<ApplicationTask[]>([]);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [dueAt, setDueAt] = useState("");
  const [reminderPreference, setReminderPreference] = useState<ReminderPreference>("none");
  const [view, setView] = useState<TaskView>("active");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [editDueAt, setEditDueAt] = useState("");
  const [editReminder, setEditReminder] = useState<ReminderPreference>("none");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setTasks(await api<ApplicationTask[]>("/api/tasks"));
  }, []);

  useEffect(() => {
    void refresh().catch((reason: Error) => setError(reason.message));
  }, [refresh]);

  async function createTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api<ApplicationTask>("/api/tasks", {
        method: "POST",
        body: JSON.stringify({ title, description, due_at: utcDue(dueAt), reminder_preference: dueAt ? reminderPreference : "none" }),
      });
      setTitle("");
      setDescription("");
      setDueAt("");
      setReminderPreference("none");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not create task.");
    } finally {
      setBusy(false);
    }
  }

  async function updateStatus(taskId: string, nextStatus: string) {
    setBusy(true);
    setError("");
    try {
      await api<ApplicationTask>(`/api/tasks/${taskId}/status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: nextStatus }),
      });
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not update task.");
    } finally {
      setBusy(false);
    }
  }

  async function completeTask(task: ApplicationTask) {
    setBusy(true);
    setError("");
    try {
      await api<ApplicationTask>(`/api/tasks/${encodeURIComponent(task.id)}/complete`, { method: "POST" });
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not complete task.");
    } finally {
      setBusy(false);
    }
  }

  async function deleteTask(task: ApplicationTask) {
    if (!window.confirm(`Delete "${task.title}"? Deleted tasks are removed from Active and Completed history.`)) return;
    setBusy(true);
    setError("");
    try {
      await api<void>(`/api/tasks/${encodeURIComponent(task.id)}`, { method: "DELETE" });
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not delete task.");
    } finally {
      setBusy(false);
    }
  }

  function beginEdit(task: ApplicationTask) {
    setEditingId(task.id);
    setEditTitle(task.title);
    setEditDescription(task.description);
    setEditDueAt(inIndiaDateTime(task.due_at));
    setEditReminder(task.reminder_preference);
  }

  async function saveEdit(event: FormEvent<HTMLFormElement>, taskId: string) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api<ApplicationTask>(`/api/tasks/${encodeURIComponent(taskId)}`, {
        method: "PUT",
        body: JSON.stringify({
          title: editTitle,
          description: editDescription,
          due_at: utcDue(editDueAt),
          reminder_preference: editDueAt ? editReminder : "none",
        }),
      });
      setEditingId(null);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not update task.");
    } finally {
      setBusy(false);
    }
  }

  const visibleTasks = useMemo(() => tasks.filter((task) => (
    view === "all" || (view === "completed" ? task.status === "done" : task.status !== "done")
  )).sort((left, right) => {
    if (!left.due_at) return right.due_at ? 1 : 0;
    if (!right.due_at) return -1;
    return new Date(left.due_at).getTime() - new Date(right.due_at).getTime();
  }), [tasks, view]);

  const reminderOptions: Array<[ReminderPreference, string]> = [
    ["none", "No reminder"],
    ["at_due", "At due time"],
    ["15_minutes", "15 minutes before"],
    ["1_hour", "1 hour before"],
    ["1_day", "1 day before"],
  ];

  return (
    <>
      <div className="page-heading"><div className="eyebrow">PERSONAL TASKS</div><h1>Task History</h1><p className="subheading">Manage deadlines, reminders, active tasks, and completed history. Deadlines are interpreted and displayed in Asia/Kolkata time.</p></div>
      {error && <div className="notice" role="alert">{error}</div>}
      <section className="panel remote-panel">
        <div className="panel-title"><h2>Create a task</h2><span className="quiet-label">{tasks.length} recorded</span></div>
        <form className="remote-form" onSubmit={(event) => void createTask(event)}>
          <label>Task title<input className="remote-input" value={title} onChange={(event) => setTitle(event.target.value)} maxLength={180} required /></label>
          <label>Description<textarea className="remote-input" value={description} onChange={(event) => setDescription(event.target.value)} maxLength={1000} rows={3} /></label>
          <label>Due date and time (Asia/Kolkata)<input className="remote-input" type="datetime-local" value={dueAt} onChange={(event) => setDueAt(event.target.value)} /></label>
          <label>Reminder<select className="remote-input" value={reminderPreference} disabled={!dueAt} onChange={(event) => setReminderPreference(event.target.value as ReminderPreference)}>{reminderOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <button className="button" disabled={busy || !title.trim()}>{busy ? "Saving..." : "Create task"}</button>
        </form>
      </section>
      <section className="panel remote-panel">
        <div className="panel-title"><h2>Tasks</h2><button className="button button-secondary" disabled={busy} onClick={() => void refresh()}>Refresh</button></div>
        <div className="task-tabs" role="tablist" aria-label="Task filters">{(["active", "completed", "all"] as const).map((filter) => <button key={filter} role="tab" aria-selected={view === filter} className={`button ${view === filter ? "" : "button-secondary"}`} onClick={() => setView(filter)}>{filter === "active" ? "Active" : filter === "completed" ? "Completed History" : "All"}</button>)}</div>
        {visibleTasks.length === 0 ? <div className="empty-state">{view === "completed" ? "No completed tasks yet." : view === "active" ? "No active tasks. Create one above." : "No tasks recorded yet."}</div> : visibleTasks.map((task) => (
          <div className="list-row task-row" key={task.id}>
            {editingId === task.id ? <form className="task-edit-form" onSubmit={(event) => void saveEdit(event, task.id)}>
              <label>Title<input className="remote-input" value={editTitle} onChange={(event) => setEditTitle(event.target.value)} required maxLength={180} /></label>
              <label>Description<textarea className="remote-input" value={editDescription} onChange={(event) => setEditDescription(event.target.value)} rows={2} maxLength={1000} /></label>
              <label>Due date/time (Asia/Kolkata)<input className="remote-input" type="datetime-local" value={editDueAt} onChange={(event) => setEditDueAt(event.target.value)} /></label>
              <label>Reminder<select className="remote-input" value={editReminder} disabled={!editDueAt} onChange={(event) => setEditReminder(event.target.value as ReminderPreference)}>{reminderOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
              <button className="button" disabled={busy || !editTitle.trim()}>{busy ? "Saving…" : "Save changes"}</button><button type="button" className="button button-secondary" disabled={busy} onClick={() => setEditingId(null)}>Cancel</button>
            </form> : <>
              <div className="list-copy"><strong>{task.title}</strong><small>{task.description || "No description"} · {task.status.replace("_", " ")} · Updated {new Date(task.updated_at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })} · Due {deadlineText(task.due_at)}{task.completed_at ? ` · Completed ${deadlineText(task.completed_at)}` : ""}</small>
                <span className={`deadline-state deadline-state--${deadlineLabel(task).toLowerCase().replace(" ", "-")}`}>{deadlineLabel(task)}</span>
              </div>
              {task.status !== "done" && <>
                <select aria-label={`Update status for ${task.title}`} className="field task-status" value={task.status} disabled={busy} onChange={(event) => void updateStatus(task.id, event.target.value)}><option value="pending">Pending</option><option value="in_progress">In progress</option><option value="blocked">Blocked</option></select>
                <button className="button" disabled={busy} onClick={() => void completeTask(task)}>Mark as completed</button>
                <button className="button button-secondary" disabled={busy} onClick={() => beginEdit(task)}>Edit</button>
              </>}
              <button className="button button-danger" disabled={busy} onClick={() => void deleteTask(task)}>Delete</button>
            </>}
          </div>
        ))}
        <p className="quiet-label">Email reminders require configured SMTP and a running backend. If the machine is offline at the scheduled time, eligible reminders are processed after it restarts.</p>
      </section>
    </>
  );
}
