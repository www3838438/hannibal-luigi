import logging
import datetime
import os
import luigi
import requests
import uuid
from luigi.contrib.docker_runner import DockerTask
logger = logging.getLogger('luigi-interface')


class MrTargetTask(DockerTask):
    '''
    Subclass the DockerTask in the Luigi Docker Runner we contributed to. This
    class:
        - points to ES as a backend
        - uses our mrtarget (ex data_pipeline) containers to run jobs.
        - relies on the local /tmp folder to store data for each docker run
    '''
    run_options = luigi.Parameter(default='-h')
    mrtargetbranch = luigi.Parameter(default='master')
    # date = luigi.DateParameter(default=datetime.date.today(),significant=False)
    data_version = luigi.Parameter(default=datetime.date.today().strftime("hannibal-%y.%m"))

    '''As we are running multiple workers, the output must be a resource that is
       accessible by all workers, such as S3/distributedFileSys or database'''

    mrtargetrepo = luigi.Parameter(default="eu.gcr.io/open-targets/mrtarget", significant=False)
    esurl = luigi.Parameter(default="http://elasticsearch:9200", significant=False)
    pubesurl = luigi.Parameter(default="http://35.189.243.117:39200", significant=False)    

    auto_remove = True
    force_pull = False
    mount_tmp = False

    @property
    def container_options(self):
        opts = self._client.create_networking_config({'esnet': self._client.create_endpoint_config()})
        logger.debug(opts)
        return {'networking_config':opts}

    @property
    def binds(self):
        logfile = '/hannibal/logs/mrtarget_' + self.run_options[0].strip('-') + '.log'
        # datadir = '/hannibal/data'
        #      
        # if not os.path.exists(datadir):
        #     os.makedirs(datadir)

        with open(os.path.expanduser(logfile), 'a'):
            os.utime(os.path.expanduser(logfile), None)

        return [os.path.expanduser(logfile) + ':/usr/src/app/output.log']
    
    @property
    def environment(self):
        ''' pass the environment variables required by the container
        '''
        return {
            "ELASTICSEARCH_NODES": self.esurl, 
            "ELASTICSEARCH_NODES_PUB": self.pubesurl,
            "CTTV_DUMP_FOLDER":"/tmp/data",
            "CTTV_DATA_VERSION": self.data_version
            
           }

    @property
    def name(self):
        return '-'.join(['mrT', self.mrtargetbranch, 
                         self.run_options[0].lstrip('-'),
                         str(uuid.uuid4().hex[:8])])

    @property
    def image(self):
        '''
        pick the container from our GCR repository
        '''
        return ':'.join([self.mrtargetrepo, self.mrtargetbranch])


    @property
    def command(self):
        return ' '.join(['mrtarget'] + self.run_options)

    def output(self):
        """
        Returns a local Target representing the inserted dataset.
        """
        taskid = '-'.join(['mrT', self.mrtargetbranch, 
                         self.run_options[0].lstrip('-'),
                         self.data_version])
        return luigi.LocalTarget('/hannibal/status/%s.done' % taskid)


    def run(self):
        '''
        extend run() of docker runner base class to touch a file target.
        '''
        DockerTask.run(self)
        self.output().open("w").close()


class DryRun(MrTargetTask):
    run_options = ['--dry-run']

class UniProt(MrTargetTask):
    run_options = ['--uni']

class Ensembl(MrTargetTask):
    run_options = ['--ens']

class Expression(MrTargetTask):
    run_options = ['--hpa']

class Reactome(MrTargetTask):
    run_options = ['--rea']

class HumanPhenotype(MrTargetTask):
    run_options = ['--hpo']

class MammalianPhenotype(MrTargetTask):
    run_options = ['--mp']

class GeneData(MrTargetTask):
    run_options = ['--gen']
    def requires(self):
        return UniProt(), Ensembl(), Expression(), Reactome(), MammalianPhenotype()

class EFO(MrTargetTask):
    run_options = ['--efo']

class ECO(MrTargetTask):
    run_options = ['--eco']

class Validate(MrTargetTask):
    '''
    Run the validation step, which takes the JSON submitted by each provider
    and makes sure they adhere to our JSON schema.
    Uses the default list contained in the pipeline
    '''
    run_options = ['--val']
    def requires(self):
        return GeneData(), Reactome(), EFO(), ECO()


class EvidenceObjects(MrTargetTask):
    '''
    Specify here the list of evidence
    Recreate evidence objects (JSON representations of each validated piece of evidence)
    and store them in the backend.
    '''

    def requires(self):
        return Validate()

    run_options = ['--evs']


class InjectedEvidence(MrTargetTask):
    '''not required by the association step, but required by the release
    '''
    def requires(self):
        return EvidenceObjects()

    run_options = ['--evs', '--inject_literature']


class AssociationObjects(MrTargetTask):
    '''the famous ass method'''
    def requires(self):
        return EvidenceObjects()

    run_options = ['--as']

class QualityControl(MrTargetTask):
    
    def requires(self):
        return AssociationObjects()

    run_options = ['--qc']

class SearchObjects(MrTargetTask):
    
    def requires(self):
        return AssociationObjects(), GeneData(), EFO()

    run_options = ['--sea']


class Relations(MrTargetTask):

    def requires(self):
        return QualityControl()

    run_options = ['--ddr']


class ReleaseSnapshot(luigi.Task):
    '''Build a snapshot of the release in gs://'''
    # parameters here only to identify the ID of the task

    mrtargetbranch = luigi.Parameter(default='master')
    data_version = luigi.Parameter(default=datetime.date.today().strftime("hannibal-%y.%m"))

    def output(self):
        taskid = '-'.join(['mrT', self.mrtargetbranch, 
                         'snapshot',
                         self.data_version])
        return luigi.LocalTarget('/hannibal/status/%s.done' % taskid)

    def requires(self):
        return Relations(), SearchObjects(), InjectedEvidence(), AssociationObjects()

    def run(self):
        snapurl = "127.0.0.1:9200/_snapshot/%s/%s?wait_for_completion=true" %\
                     (os.getenv('INSTANCE_NAME'),
                     datetime.date.today().strftime("%y%m%d-%h%M"))
        
        payload = { "indices": self.data_version + '*',
                    "ignore_unavailable": "true", 
                    "include_global_state": "false"}

        requests.put(snapurl,data = payload)




def main():
    luigi.run(["DryRun"])

if __name__ == '__main__':
    main()
