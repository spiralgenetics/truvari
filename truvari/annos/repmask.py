""" Wrapper around RepeatMasker to annotate insertion sequences in a VCF """
import os
import sys
import logging
import argparse
import tempfile
from collections import defaultdict

import pysam
import truvari
from acebinf import cmd_exe, setup_logging

# Start with just insertions and that sequence
# Eventually you can intersect with known tandem repeat regions as well
# and use those regions (with the edit) to better predict a 'difference' in number of copies
# Or you can just annotate the absolute number of the individual... probably bot

# Also, running single threaded isn't great, but also I don't care at this point...
# It'll be a heavy step, but I'm not judging the software on speed for a bit...
"""
And I need to do ALL of the elements at once because RM is much slower

And I'm going write

-pa 8 -e hmmer -species human -gff -lcambig -nocut -div 50 -no_id -s all_ins.fa
"""
DEFAULTPARAMS = "-pa {threads} -e hmmer -species human -lcambig -nocut -div 50 -no_id -s {fasta}"

def paren_int(number):
    """
    returns an integer from a string "(\d+)"
    """
    return int(number.replace('(', '').replace(')',''))

class RepMask():
    """ Class for RepeatMasker annotation """
    REPCOLS = [("RM_score", int),
               ("RM_pdiv", float),
               ("RM_pdel", float),
               ("RM_pins", float),
               ("RM_query", str),
               ("RM_qstart", int),
               ("RM_qend", int),
               ("RM_qleft", paren_int), # need custom here (it's "(int)")
               ("RM_strand", str), 
               ("RM_repeat", str),
               ("RM_clsfam", str), # need custom here.. usually class/family
               ("RM_tstart", paren_int), 
               ("RM_tend", paren_int), 
               ("RM_tleft", str)]

    def __init__(self, in_vcf, out_vcf="/dev/stdout", executable="RepeatMasker",
                 min_length=50, threshold=0.8,  rm_params=DEFAULTPARAMS, threads=1):
        """ The setup """
        self.in_vcf = in_vcf
        self.out_vcf = out_vcf
        self.executable = executable
        self.min_length = min_length
        self.threshold = threshold
        self.rm_params = rm_params
        self.threads = threads
        self.n_header = self.edit_header()
        self.cmd = f"{self.executable} {self.rm_params}"


    def edit_header(self):
        """
        New VCF INFO fields
        """
        header = None
        with pysam.VariantFile(self.in_vcf, 'r') as fh:
            header = fh.header.copy()
        # if intersect_only: Do I want to sometimes turn this off?
        # Probably, actully
        header.add_line(('##INFO=<ID=RM_score,Number=1,Type=Integer,'
                         'Description="RepMask bit score">'))
        header.add_line(('##INFO=<ID=RM_repeat,Number=1,Type=String,'
                         'Description="RepMask matching repeat">'))
        header.add_line(('##INFO=<ID=RM_clsfam,Number=1,Type=String,'
                         'Description="RepMask repeat class/family ">'))
        # TODO: Need to put a source line that says this thing was run with whatever parameters
        return header
    
    def extract_seqs(self, fout=None):
        """
        Create the fasta file of all the sequences
        Returns the fasta file
        """
        if fout is None:
            ret = tempfile.NamedTemporaryFile(mode='w', delete=False)
        else:
            ret = open(fout, 'w')
        tot_cnt = 0
        cnt = 0
        cntbp = 0
        with pysam.VariantFile(self.in_vcf) as fh:
            for pos, entry in enumerate(fh):
                tot_cnt += 1
                entry_size = truvari.entry_size(entry)
                if entry_size >= self.min_length \
                   and truvari.entry_variant_type(entry) == "INS":
                    cnt += 1
                    cntbp += entry_size
                    ret.write(f">{pos}\n{entry.alts[0]}\n")
        logging.info(f"Extracted {cnt} sequences ({cntbp}bp) from {tot_cnt} entries")
        return ret.name

    def parse_output(self, faout):
        """
        Parses the RepeatMasker output
        """
        hits = defaultdict(list)
        with open(faout, 'r') as fh:
            # header lines
            fh.readline()
            fh.readline()
            fh.readline()
            for line in fh:
                data = line.strip().split()
                data = {x[0]: x[1](y) for x, y in zip(RepMask.REPCOLS, data)}
                hits[data["RM_query"]].append(data)
        return hits

    def annotate_seqs(self, fasta):
        """
        Runs repeat masker on a fasta
        Parses the {fasta}.out
        Returns dict of {entry_pos_id: [{hitdict}, ...], ...}
        """
        logging.info("Starting RepeatMasker")
        cmd = self.cmd.format(threads=self.threads, fasta=fasta)
        ret = cmd_exe(cmd)
        if ret.ret_code != 0:
            logging.error("Couldn't run RepeatMasker")
            logging.error(str(ret))
            exit(ret.ret_code)
        logging.info("Finished RepeatMasker")
        
        hits = self.parse_output(f"{fasta}.out")
        return hits
        
    def annotate_vcf(self):
        """
        Annotates all the insertions in the vcf and writes to new vcf
        """
        #hits = self.annotate_seqs(self.extract_seqs())
        hits = self.parse_output("test.fa.out")
        with pysam.VariantFile(self.in_vcf) as fh, \
            pysam.VariantFile(self.out_vcf, 'w', header=self.n_header) as out:
            for pos, entry in enumerate(fh):
                pos = str(pos)
                if pos in hits:
                    best_hit_pct = 0
                    best_hit = None
                    entry_size = truvari.entry_size(entry)
                    for hit in hits[pos]:
                        size_aln = abs(hit["RM_qstart"] - hit["RM_qend"]) + 1
                        pct = size_aln / entry_size  # The TR that covers the most of the insertion
                        # I'm taking the single best... So I might be 'under annotating'
                        if pct >= self.threshold and pct > best_hit_pct:
                            best_hit_pct = pct
                            best_hit = hit
                    entry = self.edit_entry(entry, best_hit)
                out.write(entry)
        
    def edit_entry(self, entry, rm_hit):
        """
        puts the annos in vcf entry
        return the edited entry
        """
        if not rm_hit:
            return entry
        try:
            entry = truvari.copy_entry(entry, self.n_header)
        except TypeError:
            return entry
        
        entry.info["RM_score"] = rm_hit["RM_score"]
        entry.info["RM_repeat"] = rm_hit["RM_repeat"]
        entry.info["RM_clsfam"] = rm_hit["RM_clsfam"]

        return entry

def parse_args(args):
    """
    Pull the command line parameters
    """
    def restricted_float(x):
        x = float(x)
        if x < 0.0 or x > 1.0:
            raise argparse.ArgumentTypeError("%r not in range [0.0, 1.0]" % (x,))
        return x
    parser = argparse.ArgumentParser(prog="repmask", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-i", "--input", type=str, default="/dev/stdin",
                        help="VCF to annotate (stdin)")
    parser.add_argument("-o", "--output", type=str, default="/dev/stdout",
                        help="Output filename (stdout)")
    parser.add_argument("-e", "--executable", type=str, default="RepeatMasker",
                        help="Path to RepeatMasker (%(default)s)")
    parser.add_argument("-m", "--min-length", type=int, default=50,
                        help="Minimum size of entry to annotate (%(default)s)")
    parser.add_argument("-t", "--threshold", type=restricted_float, default=.8,
                        help="Threshold for pct of allele covered (%(default)s)")
    parser.add_argument("-p", "--params", type=str, default=DEFAULTPARAMS,
                        help="Default parameter string to send to RepeatMasker (%(default)s)")
    parser.add_argument("-T", "--threads", type=int, default=os.cpu_count(),
                        help="Number of threads to use (%(default)s)")
    parser.add_argument("--debug", action="store_true",
                        help="Verbose logging")
    args = parser.parse_args(args)
    setup_logging(args.debug)
    return args


def rmk_main(cmdargs):
    """ Main """
    args = parse_args(cmdargs)
    anno = RepMask(in_vcf=args.input,
                   out_vcf=args.output,
                   executable=args.executable,
                   min_length=args.min_length,
                   threshold=args.threshold,
                   rm_params=args.params,
                   threads=args.threads)
    anno.annotate_vcf()
    logging.info("Finished")


if __name__ == '__main__':
    test_main(sys.argv[1:])


"""
1) I can't guarantee that TRF alt seq hits are going to happend
    But I'm returning nulls - not good. need to remove I think
    | 

So 1- you can give up on the reference, totally un-needed unti you get to 'denovo mode'
Which at this point you should just abandon until it beocmes a feature request
"""
