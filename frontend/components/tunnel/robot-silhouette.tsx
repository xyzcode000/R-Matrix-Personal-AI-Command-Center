/**
 * R Matrix Bot — a CSS-only silhouette.
 *
 * Built from positioned elements so it needs no image asset, no canvas and no
 * animation library. The accent rim and visor use the existing design tokens.
 */

type RobotSilhouetteProps = Readonly<{
  /** Adds the slow visor scan used while the tunnel is playing. */
  animated?: boolean;
  className?: string;
}>;

export function RobotSilhouette({ animated = false, className = "" }: RobotSilhouetteProps) {
  return (
    <div className={`rmatrix-bot${animated ? " is-animated" : ""}${className ? ` ${className}` : ""}`} role="img" aria-label="The R Matrix Bot">
      <span className="rmatrix-bot__halo" aria-hidden="true" />
      <span className="rmatrix-bot__antenna" aria-hidden="true" />
      <span className="rmatrix-bot__head" aria-hidden="true">
        <span className="rmatrix-bot__visor" />
        <span className="rmatrix-bot__visor-scan" />
      </span>
      <span className="rmatrix-bot__neck" aria-hidden="true" />
      <span className="rmatrix-bot__torso" aria-hidden="true">
        <span className="rmatrix-bot__plate" />
        <span className="rmatrix-bot__core" />
        <span className="rmatrix-bot__vent rmatrix-bot__vent--left" />
        <span className="rmatrix-bot__vent rmatrix-bot__vent--right" />
      </span>
      <span className="rmatrix-bot__shoulder rmatrix-bot__shoulder--left" aria-hidden="true" />
      <span className="rmatrix-bot__shoulder rmatrix-bot__shoulder--right" aria-hidden="true" />
    </div>
  );
}
