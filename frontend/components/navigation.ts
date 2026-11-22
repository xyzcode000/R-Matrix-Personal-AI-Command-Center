export type NavigationGroup = "Workspace" | "Intelligence" | "System";

export type NavigationItem = Readonly<{
  href: string;
  label: string;
  mark: string;
  description: string;
  group: NavigationGroup;
  planned?: boolean;
}>;

export const navigation: readonly NavigationItem[] = [
  { href: "/dashboard", label: "Dashboard", mark: "◈", description: "Your system at a glance.", group: "Workspace" },
  { href: "/chat", label: "AI Chat", mark: "✳", description: "Think, explore, and create.", group: "Workspace" },
  { href: "/documents", label: "Documents", mark: "▤", description: "Your private knowledge archive.", group: "Workspace" },
  { href: "/projects", label: "Projects", mark: "⌘", description: "Read-only access to authorized work.", group: "Workspace" },
  { href: "/task-history", label: "Task History", mark: "◷", description: "Deadlines, reminders, and progress.", group: "Workspace" },
  { href: "/research", label: "Research", mark: "◎", description: "Current public sources, verified.", group: "Intelligence" },
  { href: "/agents", label: "Agents & Memory", mark: "✦", description: "Specialists, memories, approvals.", group: "Intelligence" },
  { href: "/remote", label: "Remote Access", mark: "⌁", description: "Enrolled devices and the control plane.", group: "System" },
  { href: "/settings", label: "Settings", mark: "⚙", description: "Configure your command center.", group: "System" },
];

export const navigationGroups: readonly NavigationGroup[] = ["Workspace", "Intelligence", "System"];

export const mirrorHallNavigation = navigation;
