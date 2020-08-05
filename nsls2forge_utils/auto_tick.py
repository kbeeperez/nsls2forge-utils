'''
This code is a rework of
https://github.com/regro/cf-scripts/blob/master/conda_forge_tick/auto_tick.py
This version was not completely importable so the functions had to be copied
here and reimplemented.
We still import some functionality from conda_forge_tick
'''
import logging
import time
import os
import glob
from urllib.error import URLError
import traceback
import json
from uuid import uuid4
from subprocess import SubprocessError, CalledProcessError

import github3
import networkx as nx

from conda_forge_tick.utils import (
    frozen_to_json_friendly,
    setup_logger,
    eval_cmd,
    dump_graph,
    load_graph,
    LazyJson
)
from conda_forge_tick.contexts import (
    MigratorContext,
    FeedstockContext,
    MigratorSessionContext
)
from conda_forge_tick.migrators import (
    Version,
    PipMigrator,
    MigrationYaml,
    LicenseMigrator,
    CondaForgeYAMLCleanup,
    ExtraJinja2KeysCleanup,
    Jinja2VarsCleanup,
)
from conda_forge_tick.auto_tick import (
    migration_factory,
    _compute_time_per_migrator,
)
from conda_forge_tick.status_report import write_version_migrator_status
from conda_forge_tick.git_utils import is_github_api_limit_reached
from conda_forge_tick.xonsh_utils import indir, env
from conda_forge_tick.mamba_solver import is_recipe_solvable

from .git_utils import (
    get_repo,
    push_repo
)

logger = logging.getLogger(__name__)

PR_LIMIT = 5
MAX_PR_LIMIT = 50

MIGRATORS = [
    Version(
        pr_limit=PR_LIMIT * 2,
        piggy_back_migrations=[
            Jinja2VarsCleanup(),
            PipMigrator(),
            LicenseMigrator(),
            CondaForgeYAMLCleanup(),
            ExtraJinja2KeysCleanup(),
        ],
    ),
]

BOT_RERUN_LABEL = {
    "name": "bot-rerun",
    "color": "#191970",
    "description": (
        "Apply this label if you want the bot to retry "
        "issuing a particular pull-request"
    ),
}


def run(feedstock_ctx, migrator, protocol='ssh', pull_request=True,
        rerender=True, fork=True, organization='nsls-ii-forge', **kwargs):
    """
    For a given feedstock and migration run the migration

    Parameters
    ----------
    feedstock_ctx: FeedstockContext
        The node attributes
    migrator: Migrator instance
        The migrator to run on the feedstock
    protocol : str, optional
        The git protocol to use, defaults to ``ssh``
    pull_request : bool, optional
        If true issue pull request, defaults to true
    rerender : bool
        Whether to rerender
    fork : bool
        If true create a fork, defaults to true
    organization: str, optional
        GitHub organization to get repo from
    gh : github3.GitHub instance, optional
        Object for communicating with GitHub, if None build from $USERNAME
        and $PASSWORD, defaults to None
    kwargs: dict
        The key word arguments to pass to the migrator

    Returns
    -------
    migrate_return: MigrationUidTypedDict
        The migration return dict used for tracking finished migrations
    pr_json: dict
        The PR json object for recreating the PR as needed
    """
    # get the repo
    migrator.attrs = feedstock_ctx.attrs

    branch_name = migrator.remote_branch(feedstock_ctx) + "_h" + uuid4().hex[0:6]

    # TODO: run this in parallel
    feedstock_dir, repo = get_repo(
        ctx=migrator.ctx.session,
        fctx=feedstock_ctx,
        branch=branch_name,
        organization=organization,
        feedstock=feedstock_ctx.feedstock_name,
        protocol=protocol,
        pull_request=pull_request,
        fork=fork,

    )

    recipe_dir = os.path.join(feedstock_dir, "recipe")

    # migrate the feedstock
    migrator.run_pre_piggyback_migrations(recipe_dir, feedstock_ctx.attrs, **kwargs)

    # TODO - make a commit here if the repo changed

    migrate_return = migrator.migrate(recipe_dir, feedstock_ctx.attrs, **kwargs)

    if not migrate_return:
        logger.critical(
            "Failed to migrate %s, %s",
            feedstock_ctx.package_name,
            feedstock_ctx.attrs.get("bad"),
        )
        eval_cmd(f"rm -rf {feedstock_dir}")
        return False, False

    # TODO - commit main migration here

    migrator.run_post_piggyback_migrations(recipe_dir, feedstock_ctx.attrs, **kwargs)

    # TODO commit post migration here

    # rerender, maybe
    diffed_files = []
    with indir(feedstock_dir), env.swap(RAISE_SUBPROC_ERROR=False):
        msg = migrator.commit_message(feedstock_ctx)  # noqa
        try:
            eval_cmd("git add --all .")
            eval_cmd(f"git commit -am '{msg}'")
        except CalledProcessError as e:
            logger.info(
                "could not commit to feedstock - "
                "likely no changes - error is '%s'" % (repr(e)),
            )
        if rerender:
            head_ref = eval_cmd("git rev-parse HEAD").strip()
            logger.info("Rerendering the feedstock")

            # In the event we can't rerender, try to update the pinnings,
            # then bail if it does not work again
            try:
                eval_cmd(
                    "conda smithy rerender -c auto --no-check-uptodate", timeout=300,
                )
            except SubprocessError:
                return False, False

            # If we tried to run the MigrationYaml and rerender did nothing (we only
            # bumped the build number and dropped a yaml file in migrations) bail
            # for instance platform specific migrations
            gdiff = eval_cmd(f"git diff --name-only {head_ref.strip()}...HEAD")

            diffed_files = [
                _
                for _ in gdiff.split()
                if not (
                    _.startswith("recipe")
                    or _.startswith("migrators")
                    or _.startswith("README")
                )
            ]

    if (
        (
            migrator.check_solvable
            and feedstock_ctx.attrs["conda-forge.yml"].get("bot", {}).get("automerge")
        )
        or feedstock_ctx.attrs["conda-forge.yml"]
        .get("bot", {})
        .get("check_solvable", False)
    ) and not is_recipe_solvable(feedstock_dir):
        eval_cmd(f"rm -rf {feedstock_dir}")
        return False, False

    if (
        isinstance(migrator, MigrationYaml)
        and not diffed_files
        and feedstock_ctx.attrs["name"] != "conda-forge-pinning"
    ):
        # spoof this so it looks like the package is done
        pr_json = {
            "state": "closed",
            "merged_at": "never issued",
            "id": str(uuid4()),
        }
    else:
        # push up
        try:
            pr_json = push_repo(
                session_ctx=migrator.ctx.session,
                fctx=feedstock_ctx,
                feedstock_dir=feedstock_dir,
                body=migrator.pr_body(feedstock_ctx),
                repo=repo,
                title=migrator.pr_title(feedstock_ctx),
                head=f"{migrator.ctx.github_username}:{branch_name}",
                branch=branch_name,
                organization=organization
            )

        # This shouldn't happen too often any more since we won't double PR
        except github3.GitHubError as e:
            if e.msg != "Validation Failed":
                raise
            else:
                print(f"Error during push {e}")
                # If we just push to the existing PR then do nothing to the json
                pr_json = False
                ljpr = False
    if pr_json:
        ljpr = LazyJson(
            os.path.join(migrator.ctx.session.prjson_dir, str(pr_json["id"]) + ".json"),
        )
        ljpr.update(**pr_json)
    # If we've gotten this far then the node is good
    feedstock_ctx.attrs["bad"] = False
    logger.info("Removing feedstock dir")
    eval_cmd(f"rm -rf {feedstock_dir}")
    return migrate_return, ljpr


def initialize_migrators(github_username="", github_password="", github_token=None,
                         dry_run=False):
    '''
    Setup graph, required contexts, and migrators

    Parameters
    ----------
    github_username: str, optional
        Username for bot on GitHub
    github_password: str, optional
        Password for bot on GitHub
    github_token: str, optional
        Token for bot on GitHub
    dry_run: bool, optional
        If true, does not submit pull requests on GitHub

    Returns
    -------
    tuple
        Migrator session to interact with GitHub,
        temporary files, and list of migrators
    '''
    temp = glob.glob("/tmp/*")
    gx = load_graph()
    smithy_version = eval_cmd("conda smithy --version").strip()
    pinning_version = json.loads(eval_cmd("conda list conda-forge-pinning --json"))[0][
        "version"
    ]
    migration_factory(MIGRATORS, gx)
    for m in MIGRATORS:
        print(f'{getattr(m, "name", m)} graph size: {len(getattr(m, "graph", []))}')

    ctx = MigratorSessionContext(
        circle_build_url=os.getenv("CIRCLE_BUILD_URL", ""),
        graph=gx,
        smithy_version=smithy_version,
        pinning_version=pinning_version,
        github_username=github_username,
        github_password=github_password,
        github_token=github_token,
        dry_run=dry_run,
    )

    return ctx, temp, MIGRATORS


def auto_tick(dry_run=False, debug=False):
    '''
    Automatically update package versions and submit pull requests to
    associated feedstocks

    Parameters
    ----------
    dry_run: bool, optional
        Generate version migration yamls but do not run them
    debug: bool, optional
        Setup logging to be in debug mode
    '''
    from conda_forge_tick.xonsh_utils import env

    if debug:
        setup_logger(logger, level="debug")
    else:
        setup_logger(logger)

    github_username = env.get("GITHUB_USERNAME", "")
    github_password = env.get("GITHUB_TOKEN", "")
    github_token = env.get("GITHUB_TOKEN")
    global MIGRATORS

    # ISSUE HERE WITH ../conda-forge-pinning-feedstock/recipe/migrations/arch_rebuild.txt
    mctx, temp, MIGRATORS = initialize_migrators(
        github_username=github_username,
        github_password=github_password,
        dry_run=dry_run,
        github_token=github_token,
    )

    # compute the time per migrator
    (num_nodes, time_per_migrator, tot_time_per_migrator) = _compute_time_per_migrator(
        mctx,
    )
    for i, migrator in enumerate(MIGRATORS):
        if hasattr(migrator, "name"):
            extra_name = "-%s" % migrator.name
        else:
            extra_name = ""

        logger.info(
            "Total migrations for %s%s: %d - gets %f seconds (%f percent)",
            migrator.__class__.__name__,
            extra_name,
            num_nodes[i],
            time_per_migrator[i],
            time_per_migrator[i] / tot_time_per_migrator * 100,
        )

    for mg_ind, migrator in enumerate(MIGRATORS):

        mmctx = MigratorContext(session=mctx, migrator=migrator)
        migrator.bind_to_ctx(mmctx)

        good_prs = 0
        _mg_start = time.time()
        effective_graph = mmctx.effective_graph
        time_per = time_per_migrator[mg_ind]

        if hasattr(migrator, "name"):
            extra_name = "-%s" % migrator.name
        else:
            extra_name = ""

        logger.info(
            "Running migrations for %s%s: %d",
            migrator.__class__.__name__,
            extra_name,
            len(effective_graph.nodes),
        )

        possible_nodes = list(migrator.order(effective_graph, mctx.graph))

        # version debugging info
        if isinstance(migrator, Version):
            logger.info("possible version migrations:")
            for node_name in possible_nodes:
                with effective_graph.nodes[node_name]["payload"] as attrs:
                    logger.info(
                        "    node|curr|new|attempts: %s|%s|%s|%d",
                        node_name,
                        attrs.get("version"),
                        attrs.get("new_version"),
                        (
                            attrs.get("new_version_attempts", {}).get(
                                attrs.get("new_version", ""), 0,
                            )
                        ),
                    )

        for node_name in possible_nodes:
            with mctx.graph.nodes[node_name]["payload"] as attrs:
                # Don't let CI timeout, break ahead of the timeout so we make certain
                # to write to the repo
                # TODO: convert these env vars
                _now = time.time()
                if (
                    (
                        _now - int(env.get("START_TIME", time.time()))
                        > int(env.get("TIMEOUT", 600))
                    )
                    or good_prs >= migrator.pr_limit
                    or (_now - _mg_start) > time_per
                ):
                    break

                fctx = FeedstockContext(
                    package_name=node_name,
                    feedstock_name=attrs["feedstock_name"],
                    attrs=attrs,
                )

                print("\n", flush=True, end="")
                logger.info(
                    "%s%s IS MIGRATING %s",
                    migrator.__class__.__name__.upper(),
                    extra_name,
                    fctx.package_name,
                )
                try:
                    # Don't bother running if we are at zero
                    if (
                        dry_run
                        or mctx.gh.rate_limit()["resources"]["core"]["remaining"] == 0
                    ):
                        break
                    migrator_uid, pr_json = run(
                        feedstock_ctx=fctx,
                        migrator=migrator,
                        rerender=migrator.rerender,
                        protocol="https",
                        hash_type=attrs.get("hash_type", "sha256"),
                    )
                    # if migration successful
                    if migrator_uid:
                        d = frozen_to_json_friendly(migrator_uid)
                        # if we have the PR already do nothing
                        if d["data"] in [
                            existing_pr["data"] for existing_pr in attrs.get("PRed", [])
                        ]:
                            pass
                        else:
                            if not pr_json:
                                pr_json = {
                                    "state": "closed",
                                    "head": {"ref": "<this_is_not_a_branch>"},
                                }
                            d["PR"] = pr_json
                            attrs.setdefault("PRed", []).append(d)
                        attrs.update(
                            {
                                "smithy_version": mctx.smithy_version,
                                "pinning_version": mctx.pinning_version,
                            },
                        )

                except github3.GitHubError as e:
                    if e.msg == "Repository was archived so is read-only.":
                        attrs["archived"] = True
                    else:
                        logger.critical(
                            "GITHUB ERROR ON FEEDSTOCK: %s", fctx.feedstock_name,
                        )
                        if is_github_api_limit_reached(e, mctx.gh):
                            break
                except URLError as e:
                    logger.exception("URLError ERROR")
                    attrs["bad"] = {
                        "exception": str(e),
                        "traceback": str(traceback.format_exc()).split("\n"),
                        "code": getattr(e, "code"),
                        "url": getattr(e, "url"),
                    }
                except Exception as e:
                    logger.exception("NON GITHUB ERROR")
                    attrs["bad"] = {
                        "exception": str(e),
                        "traceback": str(traceback.format_exc()).split("\n"),
                    }
                else:
                    if migrator_uid:
                        # On successful PR add to our counter
                        good_prs += 1
                finally:
                    # Write graph partially through
                    if not dry_run:
                        dump_graph(mctx.graph)

                    eval_cmd(f"rm -rf {mctx.rever_dir}/*")
                    logger.info(os.getcwd())
                    for f in glob.glob("/tmp/*"):
                        if f not in temp:
                            eval_cmd(f"rm -rf {f}")

    if not dry_run:
        logger.info(
            "API Calls Remaining: %d",
            mctx.gh.rate_limit()["resources"]["core"]["remaining"],
        )
    logger.info("Done")


def status_report():
    '''
    Write out the status of current/recent migrations and their
    pull requests on GitHub.

    Only works for Version migrations at the moment.
    '''
    mctx, *_, migrators = initialize_migrators()
    if not os.path.exists("./status"):
        os.mkdir("./status")

    for migrator in migrators:
        if isinstance(migrator, Version):
            write_version_migrator_status(migrator, mctx)

    lst = [
        k
        for k, v in mctx.graph.nodes.items()
        if len(
            [
                z
                for z in v.get("payload", {}).get("PRed", [])
                if z.get("PR", {}).get("state", "closed") == "open"
                and z.get("data", {}).get("migrator_name", "") == "Version"
            ],
        )
        >= Version.max_num_prs
    ]
    with open("./status/could_use_help.json", "w") as f:
        json.dump(
            sorted(
                lst,
                key=lambda z: (len(nx.descendants(mctx.graph, z)), lst),
                reverse=True,
            ),
            f,
            indent=2,
        )


def _run_handle_args(args):
    auto_tick(dry_run=args.dry_run, debug=args.debug)


def _status_handle_args(args):
    status_report()


if __name__ == '__main__':
    auto_tick(dry_run=True, debug=True)
