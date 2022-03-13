import os
import logging
import subprocess
import functools
import tempfile
import shutil


from mlflow.exceptions import MlflowException
from mlflow.utils.rest_utils import MlflowHostCreds
from databricks_cli.configure import provider
from mlflow.utils._spark_utils import _get_active_spark_session
from mlflow.utils.uri import get_db_info_from_uri

_logger = logging.getLogger(__name__)


def _use_repl_context_if_available(name):
    """
    Creates a decorator to insert a short circuit that returns the specified REPL context attribute
    if it's available.

    :param name: Attribute name (e.g. "apiUrl").
    :return: Decorator to insert the short circuit.
    """

    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            try:
                from dbruntime.databricks_repl_context import get_context

                context = get_context()
                if context is not None and hasattr(context, name):
                    return getattr(context, name)
            except Exception:
                pass
            return f(*args, **kwargs)

        return wrapper

    return decorator


def _get_dbutils():
    try:
        import IPython

        ip_shell = IPython.get_ipython()
        if ip_shell is None:
            raise _NoDbutilsError
        return ip_shell.ns_table["user_global"]["dbutils"]
    except ImportError:
        raise _NoDbutilsError
    except KeyError:
        raise _NoDbutilsError


class _NoDbutilsError(Exception):
    pass


def _get_java_dbutils():
    dbutils = _get_dbutils()
    return dbutils.notebook.entry_point.getDbutils()


def _get_command_context():
    return _get_java_dbutils().notebook().getContext()


def _get_extra_context(context_key):
    return _get_command_context().extraContext().get(context_key).get()


def _get_context_tag(context_tag_key):
    tag_opt = _get_command_context().tags().get(context_tag_key)
    if tag_opt.isDefined():
        return tag_opt.get()
    else:
        return None


@_use_repl_context_if_available("aclPathOfAclRoot")
def acl_path_of_acl_root():
    try:
        return _get_command_context().aclPathOfAclRoot().get()
    except Exception:
        return _get_extra_context("aclPathOfAclRoot")


def _get_property_from_spark_context(key):
    try:
        from pyspark import TaskContext  # pylint: disable=import-error

        task_context = TaskContext.get()
        if task_context:
            return task_context.getLocalProperty(key)
    except Exception:
        return None


def is_databricks_default_tracking_uri(tracking_uri):
    return tracking_uri.lower().strip() == "databricks"


@_use_repl_context_if_available("isInNotebook")
def is_in_databricks_notebook():
    if _get_property_from_spark_context("spark.databricks.notebook.id") is not None:
        return True
    try:
        return acl_path_of_acl_root().startswith("/workspace")
    except Exception:
        return False


@_use_repl_context_if_available("isInJob")
def is_in_databricks_job():
    try:
        return get_job_id() is not None and get_job_run_id() is not None
    except Exception:
        return False


def is_in_databricks_runtime():
    try:
        # pylint: disable=unused-import,import-error,no-name-in-module,unused-variable
        import pyspark.databricks

        return True
    except ModuleNotFoundError:
        return False


def is_dbfs_fuse_available():
    with open(os.devnull, "w") as devnull_stderr, open(os.devnull, "w") as devnull_stdout:
        try:
            return (
                subprocess.call(
                    ["mountpoint", "/dbfs"], stderr=devnull_stderr, stdout=devnull_stdout
                )
                == 0
            )
        except Exception:
            return False


@_use_repl_context_if_available("isInCluster")
def is_in_cluster():
    try:
        spark_session = _get_active_spark_session()
        return (
            spark_session is not None
            and spark_session.conf.get("spark.databricks.clusterUsageTags.clusterId") is not None
        )
    except Exception:
        return False


@_use_repl_context_if_available("notebookId")
def get_notebook_id():
    """Should only be called if is_in_databricks_notebook is true"""
    notebook_id = _get_property_from_spark_context("spark.databricks.notebook.id")
    if notebook_id is not None:
        return notebook_id
    acl_path = acl_path_of_acl_root()
    if acl_path.startswith("/workspace"):
        return acl_path.split("/")[-1]
    return None


@_use_repl_context_if_available("notebookPath")
def get_notebook_path():
    """Should only be called if is_in_databricks_notebook is true"""
    path = _get_property_from_spark_context("spark.databricks.notebook.path")
    if path is not None:
        return path
    try:
        return _get_command_context().notebookPath().get()
    except Exception:
        return _get_extra_context("notebook_path")


@_use_repl_context_if_available("runtimeVersion")
def get_databricks_runtime():
    if is_in_databricks_runtime():
        spark_session = _get_active_spark_session()
        if spark_session is not None:
            return spark_session.conf.get(
                "spark.databricks.clusterUsageTags.sparkVersion", default=None
            )
    return None


@_use_repl_context_if_available("clusterId")
def get_cluster_id():
    spark_session = _get_active_spark_session()
    if spark_session is None:
        return None
    return spark_session.conf.get("spark.databricks.clusterUsageTags.clusterId")


@_use_repl_context_if_available("jobGroupId")
def get_job_group_id():
    try:
        dbutils = _get_dbutils()
        job_group_id = dbutils.entry_point.getJobGroupId()
        if job_group_id is not None:
            return job_group_id
    except Exception:
        return None


@_use_repl_context_if_available("replId")
def get_repl_id():
    """
    :return: The ID of the current Databricks Python REPL
    """
    # Attempt to fetch the REPL ID from the Python REPL's entrypoint object. This REPL ID
    # is guaranteed to be set upon REPL startup in DBR / MLR 9.0
    try:
        dbutils = _get_dbutils()
        repl_id = dbutils.entry_point.getReplId()
        if repl_id is not None:
            return repl_id
    except Exception:
        pass

    # If the REPL ID entrypoint property is unavailable due to an older runtime version (< 9.0),
    # attempt to fetch the REPL ID from the Spark Context. This property may not be available
    # until several seconds after REPL startup
    try:
        from pyspark import SparkContext

        repl_id = SparkContext.getOrCreate().getLocalProperty("spark.databricks.replId")
        if repl_id is not None:
            return repl_id
    except Exception:
        pass


@_use_repl_context_if_available("jobId")
def get_job_id():
    try:
        return _get_command_context().jobId().get()
    except Exception:
        return _get_context_tag("jobId")


@_use_repl_context_if_available("idInJob")
def get_job_run_id():
    try:
        return _get_command_context().idInJob().get()
    except Exception:
        return _get_context_tag("idInJob")


@_use_repl_context_if_available("jobTaskType")
def get_job_type():
    """Should only be called if is_in_databricks_job is true"""
    try:
        return _get_command_context().jobTaskType().get()
    except Exception:
        return _get_context_tag("jobTaskType")


@_use_repl_context_if_available("jobType")
def get_job_type_info():
    try:
        return _get_context_tag("jobType")
    except Exception:
        return None


def get_experiment_name_from_job_id(job_id):
    return "jobs:/" + job_id


@_use_repl_context_if_available("commandRunId")
def get_command_run_id():
    try:
        return _get_command_context().commandRunId().get()
    except Exception:
        # Older runtimes may not have the commandRunId available
        return None


@_use_repl_context_if_available("apiUrl")
def get_webapp_url():
    """Should only be called if is_in_databricks_notebook or is_in_databricks_jobs is true"""
    url = _get_property_from_spark_context("spark.databricks.api.url")
    if url is not None:
        return url
    try:
        return _get_command_context().apiUrl().get()
    except Exception:
        return _get_extra_context("api_url")


@_use_repl_context_if_available("workspaceId")
def get_workspace_id():
    try:
        return _get_command_context().workspaceId().get()
    except Exception:
        return _get_context_tag("orgId")


@_use_repl_context_if_available("browserHostName")
def get_browser_hostname():
    try:
        return _get_command_context().browserHostName().get()
    except Exception:
        return _get_context_tag("browserHostName")


def get_workspace_info_from_dbutils():
    try:
        dbutils = _get_dbutils()
        if dbutils:
            browser_hostname = get_browser_hostname()
            workspace_host = "https://" + browser_hostname if browser_hostname else get_webapp_url()
            workspace_id = get_workspace_id()
            return workspace_host, workspace_id
    except Exception:
        pass
    return None, None


@_use_repl_context_if_available("workspaceUrl")
def get_workspace_url():
    try:
        spark_session = _get_active_spark_session()
        if spark_session is not None:
            return spark_session.conf.get("spark.databricks.workspaceUrl")
    except Exception:
        return None


def get_workspace_info_from_databricks_secrets(tracking_uri):
    profile, key_prefix = get_db_info_from_uri(tracking_uri)
    if key_prefix:
        dbutils = _get_dbutils()
        if dbutils:
            workspace_id = dbutils.secrets.get(scope=profile, key=key_prefix + "-workspace-id")
            workspace_host = dbutils.secrets.get(scope=profile, key=key_prefix + "-host")
            return workspace_host, workspace_id
    return None, None


def _fail_malformed_databricks_auth(profile):
    raise MlflowException(
        "Got malformed Databricks CLI profile '%s'. Please make sure the "
        "Databricks CLI is properly configured as described at "
        "https://github.com/databricks/databricks-cli." % profile
    )


def get_databricks_host_creds(server_uri=None):
    """
    Reads in configuration necessary to make HTTP requests to a Databricks server. This
    uses the Databricks CLI's ConfigProvider interface to load the DatabricksConfig object.
    If no Databricks CLI profile is found corresponding to the server URI, this function
    will attempt to retrieve these credentials from the Databricks Secret Manager. For that to work,
    the server URI will need to be of the following format: "databricks://scope:prefix". In the
    Databricks Secret Manager, we will query for a secret in the scope "<scope>" for secrets with
    keys of the form "<prefix>-host" and "<prefix>-token". Note that this prefix *cannot* be empty
    if trying to authenticate with this method. If found, those host credentials will be used. This
    method will throw an exception if sufficient auth cannot be found.

    :param server_uri: A URI that specifies the Databricks profile you want to use for making
    requests.
    :return: :py:class:`mlflow.rest_utils.MlflowHostCreds` which includes the hostname and
        authentication information necessary to talk to the Databricks server.
    """
    profile, path = get_db_info_from_uri(server_uri)
    if not hasattr(provider, "get_config"):
        _logger.warning(
            "Support for databricks-cli<0.8.0 is deprecated and will be removed"
            " in a future version."
        )
        config = provider.get_config_for_profile(profile)
    elif profile:
        config = provider.ProfileConfigProvider(profile).get_config()
    else:
        config = provider.get_config()
    # if a path is specified, that implies a Databricks tracking URI of the form:
    # databricks://profile-name/path-specifier
    if (not config or not config.host) and path:
        dbutils = _get_dbutils()
        if dbutils:
            # Prefix differentiates users and is provided as path information in the URI
            key_prefix = path
            host = dbutils.secrets.get(scope=profile, key=key_prefix + "-host")
            token = dbutils.secrets.get(scope=profile, key=key_prefix + "-token")
            if host and token:
                config = provider.DatabricksConfig.from_token(
                    host=host, token=token, insecure=False
                )
    if not config or not config.host:
        _fail_malformed_databricks_auth(profile)

    insecure = hasattr(config, "insecure") and config.insecure

    if config.username is not None and config.password is not None:
        return MlflowHostCreds(
            config.host,
            username=config.username,
            password=config.password,
            ignore_tls_verification=insecure,
        )
    elif config.token:
        return MlflowHostCreds(config.host, token=config.token, ignore_tls_verification=insecure)
    _fail_malformed_databricks_auth(profile)


def _run_command(cmd):
    """
    Runs the specified command. If it exits with non-zero status, `MlflowException` is raised.
    """
    print(f'running command {" ".join(cmd)}')
    proc = subprocess.Popen(cmd)
    proc.communicate()
    if proc.returncode != 0:
        msg = "Encountered an unexpected error while building the wheel."
        raise MlflowException(msg)


def _create_or_update_wheel(pip_requirements, run_id, experiment_id, path):
    from mlflow.utils.proto_json_utils import message_to_json
    from mlflow.utils.rest_utils import call_endpoint

    requirements = "\n".join(pip_requirements)
    from mlflow.protos.databricks_artifacts_pb2 import SetWheelUri

    req_body = message_to_json(
        SetWheelUri(
            requirements=requirements, wheel_id=run_id, experiment_id=experiment_id, wheel_uri=path
        )
    )
    response = call_endpoint(
        get_databricks_host_creds(),
        "/api/2.0/mlflow/endpoints-v2/set-wheel-uri",
        "PUT",
        req_body,
        SetWheelUri.Response(),
    )
    print("response", response)


def build_and_upload_model_serving_wheel(pip_requirements, extra_index_url=None, find_links=None):
    import sys
    import mlflow

    experiment_name = "/Shared/ModelWheels"
    experiment = mlflow.get_experiment_by_name(experiment_name)
    if experiment is None:
        experiment_id = mlflow.create_experiment(experiment_name)
    else:
        experiment_id = experiment.experiment_id

    with tempfile.TemporaryDirectory() as tmp_dir_path:
        reqs = "\n".join(pip_requirements)
        req_path = os.path.join(tmp_dir_path, "requirements.txt")
        wheels_path = os.path.join(tmp_dir_path, "wheels")
        with open(req_path, "w") as f:
            f.write(reqs)
        cmd = [sys.executable, "-m", "pip", "wheel", "--wheel-dir", wheels_path, "-r", req_path]
        if extra_index_url:
            cmd.extend(("--extra-index-url", extra_index_url))
        if find_links:
            cmd.extend(("--find-links", find_links))
        _run_command(cmd)
        wheels_zip = os.path.join(tmp_dir_path, "wheels.zip")
        shutil.make_archive(wheels_path, root_dir=wheels_path, format="zip")
        with mlflow.start_run(experiment_id=experiment_id) as run:
            mlflow.log_artifact(wheels_zip)
        run_id = run.info.run_id
        _create_or_update_wheel(pip_requirements, run_id, experiment_id, "wheels.zip")
