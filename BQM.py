from __future__ import print_function

from re import match
from bisect import bisect_right

import dwavebinarycsp


def get_jss_bqm(job_dict, max_time=None, stitch_kwargs=None):
    """Returns a BQM to the  Scheduling problem.
    """
    if stitch_kwargs == None:
        stitch_kwargs = {}

    scheduler = JobShopScheduler(job_dict, max_time)
    return scheduler.get_bqm(stitch_kwargs)


def sum_to_one(*args):
    return sum(args) == 1


def get_label(task, time):
    """Creates a standardized name for variables in the constraint satisfaction problem
    """
    return "{task.job}_{task.position},{time}".format(**locals())


class Task:
    def __init__(self, job, position, machine, duration):
        self.job = job
        self.position = position
        self.machine = machine
        self.duration = duration

    def __repr__(self):
        return ("{{job: {job}, position: {position}, machine: {machine}, duration:"
                " {duration}}}").format(**vars(self))


class KeyList:
    """A wrapper to an array. Used for passing the key of a custom object to the bisect function.
    Note: bisect function does not let you choose an arbitrary key, hence this class was created.
    """

    def __init__(self, array, key_function):
        self.array = array  # An iterable
        self.key_function = key_function  # Function for grabbing the key of a given item

    def __len__(self):
        return len(self.array)

    def __getitem__(self, index):
        item = self.array[index]
        key = self.key_function(item)
        return key


class JobShopScheduler:
    def __init__(self, job_dict, max_time=None):
        """
        Args:
            job_dict: A dictionary. It describes the jobs that need to be scheduled. Namely, the
              dict key is the name of the job and the dict value is the ordered list of tasks that
              the job must do. (See Job Dict Details below.)
            max_time: An integer. The upper bound on the amount of time the schedule can take.
        Job Dict Details:
            The job_dict has the following format:
              {"job_name": [(machine_name, integer_time_duration_on_machine), ..],
               ..
               "another_job_name": [(some_machine, integer_time_duration_on_machine), ..]}
            A small job_dict example:
              jobs = {"job_a": [("mach_1", 2), ("mach_2", 2), ("mach_3", 2)],
                      "job_b": [("mach_3", 3), ("mach_2", 1), ("mach_1", 1)],
                      "job_c": [("mach_2", 2), ("mach_1", 3), ("mach_2", 1)]}
        """

        self.tasks = []
        self.last_task_indices = []
        self.max_time = max_time    # will get decremented by 1 for zero-indexing; see _process_data
        self.csp = dwavebinarycsp.ConstraintSatisfactionProblem(dwavebinarycsp.BINARY)

        # Populates self.tasks and self.max_time
        self._process_data(job_dict)

    def _process_data(self, jobs):
        """Process user input into a format that is more convenient for JobShopScheduler functions.
        """
        # Create and concatenate Task objects
        tasks = []
        last_task_indices = [-1]    # -1 for zero-indexing
        total_time = 0  # total time of all jobs
        max_job_time = 0

        for job_name, job_tasks in jobs.items():
            last_task_indices.append(last_task_indices[-1] + len(job_tasks))
            job_time = 0

            for i, (machine, time_span) in enumerate(job_tasks):
                tasks.append(Task(job_name, i, machine, time_span))
                total_time += time_span
                job_time += time_span

            # Store the time of the longest running job
            if job_time > max_job_time:
                max_job_time = job_time

        # Update values
        # Note: max_job_time is a lowerbound to the time it takes for the optimal schedule. This is
        #   because the longest job must be a part of this optimal schedule.
        self.tasks = tasks
        self.last_task_indices = last_task_indices[1:]
        self.max_job_time = max_job_time - 1    # -1 to account for zero-indexing

        if self.max_time is None:
            self.max_time = total_time
        self.max_time -= 1    # -1 to account for zero-indexing

    def _add_one_start_constraint(self):
        """self.csp gets the constraint: A task can start once and only once
        """
        for task in self.tasks:
            task_times = {get_label(task, t) for t in range(self.max_time + 1)}
            self.csp.add_constraint(sum_to_one, task_times)

    def _add_precedence_constraint(self):
        """self.csp gets the constraint: Task must follow a particular order.
         Note: assumes self.tasks are sorted by jobs and then by position
        """
        valid_edges = {(0, 0), (1, 0), (0, 1)}
        for current_task, next_task in zip(self.tasks, self.tasks[1:]):
            if current_task.job != next_task.job:
                continue

            # Forming constraints with the relevant times of the next task
            for t in range(self.max_time + 1):
                current_label = get_label(current_task, t)

                for tt in range(min(t + current_task.duration, self.max_time + 1)):
                    next_label = get_label(next_task, tt)
                    self.csp.add_constraint(valid_edges, {current_label, next_label})

    def _add_share_machine_constraint(self):
        """self.csp gets the constraint: At most one task per machine per time
        """
        sorted_tasks = sorted(self.tasks, key=lambda x: x.machine)
        wrapped_tasks = KeyList(sorted_tasks, lambda x: x.machine) # Key wrapper for bisect function

        head = 0
        valid_values = {(0, 0), (1, 0), (0, 1)}
        while head < len(sorted_tasks):

            # Find tasks that share a machine
            tail = bisect_right(wrapped_tasks, sorted_tasks[head].machine)
            same_machine_tasks = sorted_tasks[head:tail]

            # Update
            head = tail

            # No need to build coupling for a single task
            if len(same_machine_tasks) < 2:
                continue

            # Apply constraint between all tasks for each unit of time
            for task in same_machine_tasks:
                for other_task in same_machine_tasks:
                    if task.job == other_task.job and task.position == other_task.position:
                        continue

                    for t in range(self.max_time + 1):
                        current_label = get_label(task, t)

                        for tt in range(t, min(t + task.duration, self.max_time + 1)):
                            self.csp.add_constraint(valid_values, {current_label,
                                                                   get_label(other_task, tt)})

    def _remove_absurd_times(self):
        """Sets impossible task times in self.csp to 0.
        """
        # Times that are too early for task
        predecessor_time = 0
        current_job = self.tasks[0].job
        for task in self.tasks:
            # Check if task is in current_job
            if task.job != current_job:
                predecessor_time = 0
                current_job = task.job

            for t in range(predecessor_time):
                label = get_label(task, t)
                self.csp.fix_variable(label, 0)

            predecessor_time += task.duration

        # Times that are too late for task
        # Note: we are going through the task list backwards in order to compute
        # the successor time
        successor_time = -1    # start with -1 so that we get (total task time - 1)
        current_job = self.tasks[-1].job
        for task in self.tasks[::-1]:
            # Check if task is in current_job
            if task.job != current_job:
                successor_time = -1
                current_job = task.job

            successor_time += task.duration
            for t in range(successor_time):
                label = get_label(task, self.max_time - t)
                self.csp.fix_variable(label, 0)

    def _edit_bqm_for_shortest_schedule(self, bqm):

        base = len(self.last_task_indices) + 1     # Base for exponent
        # Get our pruned (remove_absurd_times) variable list so we don't undo pruning
        pruned_variables = list(bqm.variables)
        for i in self.last_task_indices:
            task = self.tasks[i]

            for t in range(self.max_time + 1):
                end_time = t + task.duration - 1    # -1 to get last unit of time the task occupies

                # Check task's end time
                # Note: first condition is to prevent adding in absurd times. Second condition is
                #   to prevent penalizing job end-times shorter than the shortest possible schedule
                #   end-time (i.e. the time it takes to run the longest job).
                if end_time > self.max_time or end_time <= self.max_job_time:
                    continue

                # Add bias to variable
                # Note: the bias shown here is a scaled version of the proof shown above. Rather
                #   than simply doing base**end_time, I have scaled the all biases with
                #   2 / base**self.max_time. This way, the largest possible bias
                #   (when end_time==(self.max_time-1)) is 2.
                bias = 2 * base**(end_time - self.max_time)
                label = get_label(task, t)
                if label in pruned_variables:
                    bqm.add_variable(label, bias)

    def get_bqm(self, stitch_kwargs=None):
        """Returns a BQM to the Job Shop Scheduling problem.
        Args:
            stitch_kwargs: A dict. Kwargs to be passed to dwavebinarycsp.stitch.
        """
        if stitch_kwargs is None:
            stitch_kwargs = {}

        # Apply constraints to self.csp
        self._add_one_start_constraint()
        self._add_precedence_constraint()
        self._add_share_machine_constraint()
        self._remove_absurd_times()

        # Get BQM
        bqm = dwavebinarycsp.stitch(self.csp, **stitch_kwargs)

        # Edit BQM to encourage an optimal schedule
        self._edit_bqm_for_shortest_schedule(bqm)

        return bqm


def is_auxiliary_variable(v):
    """Check whether named variable is an auxiliary variable.
    Auxiliary variables may be added as part of converting the
    constraint satisfaction problem to a BQM.
    """
    return match("aux\d+$", v)