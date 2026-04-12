import { ReviewRoute } from "@/components/review-route";


type PageProps = {
  params: Promise<{
    owner: string;
    repo: string;
    pullNumber: string;
  }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};


export default async function LatestReviewPage({ params, searchParams }: PageProps) {
  const routeParams = await params;
  const resolvedSearchParams = await searchParams;
  const pullNumber = Number(routeParams.pullNumber);

  return (
    <ReviewRoute
      currentPath={`/reviews/${routeParams.owner}/${routeParams.repo}/pulls/${pullNumber}`}
      owner={routeParams.owner}
      pullNumber={pullNumber}
      repo={routeParams.repo}
      searchParams={resolvedSearchParams}
    />
  );
}
