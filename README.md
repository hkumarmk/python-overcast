# Overcast Deployment Engine

![Travis status](https://travis-ci.org/overcastde/python-overcast.svg)

This is the Overcast Deployment Engine. It's a cloud centric deployment engine
that aims to facilitate deployment for both ephemeral environments (for
integration testing) as well for long-lived environments. Support for the
latter is still a little sketchy, but it's improving quickly.

Overcast learns from a YAML file what needs to be done for your deployment.
Here's the simplest possible example:

    main:
      - shell:
          cmd: "echo Hello, world"

This defines a sequence of steps called "main". It could be named anything
you like and you can have several in the same file.

The main sequence in our example only has a single step: A shell step. It executes a single command which prints `Hello, world` and exits.

You can also run commands on remote hosts:

    main:
      - shell:
        type: remote
        node: web1
        cmd: "echo Hello, world"

The output will be the same as in the previous example, but this time it will
be executed on a remote host named web1.

The shell step type has a number of attributes:

- `cmd`: We've already seen this. It's the command to run. It's mandatory.
- `type`: Can be set to "remote" if it's to be run remotely. Optional.
- `node`: Specifies the remote host to run on if type: remote. Mandatory if type is "remote".
- `retry-if-fails`: A boolean specifying whether to retry if the cmd fails.
- `retry-delay`: Time to wait between retries. An integer will be treated as seconds. You can append `s`, `m`, `h` as suffixes. They do what you think they do.
- `timeout`: Timeout for each command run. It will be terminated if it takes longer than this and will be considered a failure.
- `total-timeout`: A timeout for all executions of this command (useful if you `retry-if-fails`).

The other step type is "`provision`". This is where it gets interesting.

The provision step type has only two attributes:
 - `stack`: name of another yaml file describing the stack you want to deploy.
 - `userdata`: name of a file that holds the userdata you want to pass to your shiny, new instances. It doesn't have to exist when we start running the sequence, so you can generate it in a preceding shell step.

Let's look at an example stack file:

    nodes:
      bootstrap:
        flavor: bootstrap
        image: trusty
        disk: 10
        networks:
        - network: undercloud
          securitygroups:
          - jumphost
          - undercloud
    networks:
      undercloud:
        cidr: 10.130.182.0/24
    securitygroups:
      jumphost:
      - cidr: 0.0.0.0/0
        from_port: 22
        to_port: 22
        protocol: tcp
      undercloud:

This particular stack file describes a number of different resoruces that constitute your deployment.
 - Two security groups:  `jumphost` and `undercloud`. `jumphost` has a single rule: Allow SSH from anywhere. `undercloud` has no rules. It exists to allow different hosts in that security group to communicate freely.
 - One network: `undercloud`
 - One node: `bootstrap`

First all networks (and their subnets) are created. Then security groups. Then nodes. To make sure security groups are in effect at all times, ports are pre-created with their relevant config and then passed to the API call to create the instances.

You'll notice that `flavor` and `image` have human readable names. That's because these stack definitions should be agnostic to which cloud you're deploying to. To map these values to their correct values for a given cloud provider, a mapping file is passed in.
 
## Invoking Overcast

Let's look at how you actually use all of this.

    $ cd examples; overcast deploy --cfg test.yaml \
                                   --key ${HOME}/.ssh/id_rsa.pub \
                                   --mappings mappings.ini \
                                   --suffix test1234 \
                                   --cleanup cleanup.log \
                                   main

`overcast` expects you to have some environment variables set to be able to authenticate. They are `OS_USERNAME`, `OS_PASSWORD`, `OS_TENANT_NAME`, `OS_AUTH_URL`. Their expected value should be fairly obvious.

We're passing in a mapping file: `mappings.ini`. Here's an example that matches the example stack file above:

    [flavors]
    bootstrap = 92065143-073f-4d88-b75e-360ae5c12eac

    [images]
    trusty = e824592a-8265-4e32-98d9-8c20c3e19f7a

Whenever a stack file references a flavor called "bootstrap", the mappings file provides a translation to a flavor ID specific to your target cloud. Same for images.
