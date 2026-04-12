import { notFound } from "next/navigation";

import { AiGatewaySettings } from "@/components/ai-gateway-settings";
import { ApiRequestError, buildLoginHref, getAiGatewaySettings, getReviewWorkspace } from "@/lib/api";
import { buildAiGatewayRoute } from "@/lib/review-workspace";


type PageProps = {
  params: Promise<{
    owner: string;
    repo: string;
    pullNumber: string;
  }>;
};


export default async function AiGatewaySettingsPage({ params }: PageProps) {
  const routeParams = await params;
  const pullNumber = Number(routeParams.pullNumber);
  const currentPath = buildAiGatewayRoute(
    routeParams.owner,
    routeParams.repo,
    pullNumber,
  );

  try {
    const workspace = await getReviewWorkspace(
      routeParams.owner,
      routeParams.repo,
      pullNumber,
    );
    const settings = await getAiGatewaySettings(workspace.review.installation.id);

    return (
      <AiGatewaySettings
        config={settings.config}
        currentPath={currentPath}
        review={workspace.review}
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
        <p className="eyebrow">Managed AI Settings</p>
        <h1>Sign in with GitHub to manage installation settings</h1>
        <p className="hero-summary">
          NotebookLens requires an authenticated GitHub session before it can
          verify installation-admin access for LiteLLM settings.
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
        <p className="eyebrow">Settings Error</p>
        <h1>NotebookLens could not load this installation configuration</h1>
        <p className="hero-summary">{detail}</p>
      </section>
    </main>
  );
}
