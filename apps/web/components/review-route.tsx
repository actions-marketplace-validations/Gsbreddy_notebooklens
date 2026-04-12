import { notFound } from "next/navigation";

import { ApiRequestError, buildLoginHref, getReviewWorkspace, getSnapshotWorkspace } from "@/lib/api";
import { readFlashNotice } from "@/lib/review-workspace";
import { ReviewWorkspace } from "@/components/review-workspace";


type ReviewRouteProps = {
  owner: string;
  repo: string;
  pullNumber: number;
  snapshotIndex?: number;
  currentPath: string;
  searchParams: Record<string, string | string[] | undefined>;
};


export async function ReviewRoute(props: ReviewRouteProps) {
  const { currentPath, owner, pullNumber, repo, searchParams, snapshotIndex } = props;

  try {
    const workspace =
      snapshotIndex === undefined
        ? await getReviewWorkspace(owner, repo, pullNumber)
        : await getSnapshotWorkspace(owner, repo, pullNumber, snapshotIndex);

    return (
      <ReviewWorkspace
        currentPath={currentPath}
        flashNotice={readFlashNotice(searchParams)}
        workspace={workspace}
      />
    );
  } catch (error) {
    if (error instanceof ApiRequestError) {
      if (error.status === 401) {
        return <AuthWall currentPath={currentPath} />;
      }

      if (error.status === 404) {
        notFound();
      }

      return <ErrorWall detail={error.detail} />;
    }

    throw error;
  }
}


function AuthWall({ currentPath }: { currentPath: string }) {
  return (
    <main className="center-stage">
      <section className="hero-card compact-card">
        <p className="eyebrow">Managed Review Access</p>
        <h1>Sign in with GitHub to open this review workspace</h1>
        <p className="hero-summary">
          NotebookLens checks repository visibility with your GitHub OAuth session
          before it loads snapshot history or inline thread data.
        </p>
        <a className="primary-button" href={buildLoginHref(currentPath)}>
          Continue with GitHub
        </a>
      </section>
    </main>
  );
}


function ErrorWall({ detail }: { detail: string }) {
  return (
    <main className="center-stage">
      <section className="hero-card compact-card">
        <p className="eyebrow">Workspace Error</p>
        <h1>NotebookLens could not load this review</h1>
        <p className="hero-summary">{detail}</p>
      </section>
    </main>
  );
}
