import os
import signal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from buildbot.changes.base import ChangeSource
from buildbot.config.builder import BuilderConfig
from buildbot.plugins import util
from buildbot.reporters.base import ReporterBase
from buildbot.reporters.gitlab import GitLabStatusPush
from buildbot.www.auth import AuthBase
from buildbot.www.avatar import AvatarBase
from pydantic import BaseModel
from twisted.logger import Logger
from twisted.python import log

from buildbot_nix.common import (
    ThreadDeferredBuildStep,
    atomic_write_file,
    filter_repos_by_topic,
    http_request,
    model_dump_project_cache,
    model_validate_project_cache,
    paginated_github_request,
    slugify_project_name,
)
from buildbot_nix.models import GitlabConfig, Interpolate
from buildbot_nix.nix_status_generator import BuildNixEvalStatusGenerator
from buildbot_nix.projects import GitBackend, GitProject

tlog = Logger()


class NamespaceData(BaseModel):
    path: str
    kind: str


class RepoData(BaseModel):
    id: int
    name_with_namespace: str
    path: str
    path_with_namespace: str
    ssh_url_to_repo: str
    web_url: str
    namespace: NamespaceData
    default_branch: str
    topics: list[str]


class GitlabProject(GitProject):
    config: GitlabConfig
    data: RepoData

    def __init__(self, config: GitlabConfig, data: RepoData) -> None:
        self.config = config
        self.data = data

    def get_project_url(self) -> str:
        url = urlparse(self.config.instance_url)
        return f"{url.scheme}://git:%(secret:{self.config.token_file})s@{url.hostname}/{self.data.path_with_namespace}"

    def create_change_source(self) -> ChangeSource | None:
        return None

    @property
    def pretty_type(self) -> str:
        return "Gitlab"

    @property
    def type(self) -> str:
        return "gitlab"

    @property
    def nix_ref_type(self) -> str:
        return "gitlab"

    @property
    def repo(self) -> str:
        return self.data.path

    @property
    def owner(self) -> str:
        return self.data.namespace.path

    @property
    def name(self) -> str:
        return self.data.name_with_namespace

    @property
    def url(self) -> str:
        return self.data.web_url

    @property
    def project_id(self) -> str:
        return slugify_project_name(self.data.path_with_namespace)

    @property
    def default_branch(self) -> str:
        return self.data.default_branch

    @property
    def topics(self) -> list[str]:
        return self.data.topics

    @property
    def belongs_to_org(self) -> bool:
        return self.data.namespace.kind == "group"

    @property
    def private_key_path(self) -> Path | None:
        return None

    @property
    def known_hosts_path(self) -> Path | None:
        return None


class GitlabBackend(GitBackend):
    config: GitlabConfig
    instance_url: str

    def __init__(self, config: GitlabConfig, instance_url: str) -> None:
        self.config = config
        self.instance_url = instance_url

    def create_reload_builder(self, worker_names: list[str]) -> BuilderConfig:
        factory = util.BuildFactory()
        factory.addStep(
            ReloadGitlabProjects(self.config, self.config.project_cache_file),
        )
        factory.addStep(
            CreateGitlabProjectHooks(
                self.config,
                self.instance_url,
            )
        )
        return util.BuilderConfig(
            name=self.reload_builder_name,
            workernames=worker_names,
            factory=factory,
        )

    def create_reporter(self) -> ReporterBase:
        return GitLabStatusPush(
            token=self.config.token,
            context=Interpolate("buildbot/%(prop:status_name)s"),
            baseURL=self.config.instance_url,
            generators=[
                BuildNixEvalStatusGenerator(),
            ],
        )

    def create_change_hook(self) -> dict[str, Any]:
        return dict(secret=self.config.webhook_secret)

    def load_projects(self) -> list["GitProject"]:
        if not self.config.project_cache_file.exists():
            return []

        repos: list[RepoData] = filter_repos_by_topic(
            self.config.topic,
            sorted(
                model_validate_project_cache(RepoData, self.config.project_cache_file),
                key=lambda repo: repo.path_with_namespace,
            ),
            lambda repo: repo.topics,
        )
        tlog.info(f"Loading {len(repos)} cached repos.")

        return [
            GitlabProject(self.config, RepoData.model_validate(repo)) for repo in repos
        ]

    def are_projects_cached(self) -> bool:
        return self.config.project_cache_file.exists()

    def create_auth(self) -> AuthBase:
        raise NotImplementedError

    def create_avatar_method(self) -> AvatarBase | None:
        return None

    @property
    def reload_builder_name(self) -> str:
        return "reload-gitlab-projects"

    @property
    def type(self) -> str:
        return "gitlab"

    @property
    def pretty_type(self) -> str:
        return "Gitlab"

    @property
    def change_hook_name(self) -> str:
        return "gitlab"


class ReloadGitlabProjects(ThreadDeferredBuildStep):
    name = "reload_gitlab_projects"

    config: GitlabConfig
    project_cache_file: Path

    def __init__(
        self,
        config: GitlabConfig,
        project_cache_file: Path,
        **kwargs: Any,
    ) -> None:
        self.config = config
        self.project_cache_file = project_cache_file
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        repos: list[RepoData] = filter_repos_by_topic(
            self.config.topic,
            refresh_projects(self.config, self.project_cache_file),
            lambda repo: repo.topics,
        )
        atomic_write_file(self.project_cache_file, model_dump_project_cache(repos))

    def run_post(self) -> Any:
        return util.SUCCESS


class CreateGitlabProjectHooks(ThreadDeferredBuildStep):
    name = "create_gitlab_project_hooks"

    config: GitlabConfig
    instance_url: str

    def __init__(self, config: GitlabConfig, instance_url: str, **kwargs: Any) -> None:
        self.config = config
        self.instance_url = instance_url
        super().__init__(**kwargs)

    def run_deferred(self) -> None:
        repos = model_validate_project_cache(RepoData, self.config.project_cache_file)
        for repo in repos:
            create_project_hook(
                token=self.config.token,
                webhook_secret=self.config.webhook_secret,
                project_id=repo.id,
                gitlab_url=self.config.instance_url,
                instance_url=self.instance_url,
            )

    def run_post(self) -> Any:
        os.kill(os.getpid(), signal.SIGHUP)
        return util.SUCCESS


def refresh_projects(config: GitlabConfig, cache_file: Path) -> list[RepoData]:
    # access level 40 == Maintainer. See https://docs.gitlab.com/api/members/#roles
    return [
        RepoData.model_validate(repo)
        for repo in paginated_github_request(
            f"{config.instance_url}/api/v4/projects?min_access_level=40&pagination=keyset&per_page=100&order_by=id&sort=asc",
            config.token,
        )
    ]


def create_project_hook(
    token: str,
    webhook_secret: str,
    project_id: int,
    gitlab_url: str,
    instance_url: str,
) -> None:
    hook_url = instance_url + "change_hook/gitlab"
    for hook in paginated_github_request(
        f"{gitlab_url}/api/v4/projects/{project_id}/hooks",
        token,
    ):
        if hook["url"] == hook_url:
            log.msg(f"hook for gitlab project {project_id} already exists")
            return
    log.msg(f"creating hook for gitlab project {project_id}")
    http_request(
        f"{gitlab_url}/api/v4/projects/{project_id}/hooks",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        data=dict(
            name="buildbot hook",
            url=hook_url,
            enable_ssl_verification=True,
            token=webhook_secret,
            # We don't need to be informed of most events
            confidential_issues_events=False,
            confidential_note_events=False,
            deployment_events=False,
            feature_flag_events=False,
            issues_events=False,
            job_events=False,
            merge_requests_events=False,
            note_events=False,
            pipeline_events=False,
            releases_events=False,
            wiki_page_events=False,
            resource_access_token_events=False,
        ),
    )
