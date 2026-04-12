import { ReviewRoute } from "@/components/review-route";


type PageProps = {
  params: Promise<{
    owner: string;
    repo: string;
    pullNumber: string;
    snapshotIndex: string;
  }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};


export default async function SnapshotReviewPage({ params, searchParams }: PageProps) {
  const routeParams = await params;
  const resolvedSearchParams = await searchParams;
  const pullNumber = Number(routeParams.pullNumber);
  const snapshotIndex = Number(routeParams.snapshotIndex);

  return (
    <ReviewRoute
      currentPath={`/reviews/${routeParams.owner}/${routeParams.repo}/pulls/${pullNumber}/snapshots/${snapshotIndex}`}
      owner={routeParams.owner}
      pullNumber={pullNumber}
      repo={routeParams.repo}
      searchParams={resolvedSearchParams}
      snapshotIndex={snapshotIndex}
    />
  );
}
