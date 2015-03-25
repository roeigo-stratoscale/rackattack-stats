MODULE_DIRNAME = $(shell basename `pwd`)
MODULE_NAME = ${subst -,.,$(MODULE_DIRNAME)}
EGG_BASENAME = ${MODULE_NAME}.egg
SERVICES_FILENAMES = $(shell find -maxdepth 1 -name "*.service" | sed 's/.\///g')
PYTHON_FILES = $(shell find rackattack -name "*.py")
MAIN_FILES = $(shell find rackattack -name "*main*.py")

all: build check_convention

check_convention:
	pep8 rackattack --max-line-length=109

.PHONY: build
build: build/$(EGG_BASENAME)

build/${EGG_BASENAME}: ${PYTHON_FILES}
	mkdir -p $(@D)
	python -m upseto.packegg --entryPoint ${MAIN_FILES} --output=$@ --createDeps=$@.dep --compile_pyc --joinPythonNamespaces

-include build/$(EGG_BASENAME).dep

install: build/$(EGG_BASENAME)
	-for _service in ${SERVICES_FILENAMES} ; do \
		sudo systemctl stop $$_service ; \
	done
	-sudo mkdir /usr/share/$(MODULE_NAME)
	sudo cp build/$(EGG_BASENAME) /usr/share/$(MODULE_NAME)
	for _service in ${SERVICES_FILENAMES} ; do \
		sudo cp $$_service /usr/lib/systemd/system/ ; \
		sudo systemctl enable $$_service ; \
		(if ["$(DONT_START_SERVICE)" == ""]; then sudo systemctl start $$_service; fi ) ; \
	done

uninstall:
#	-sudo systemctl stop $(SERVICE_BASENAME)
#	-sudo systemctl disable $(SERVICE_BASENAME)
#	-sudo rm -fr /usr/lib/systemd/system/$(SERVICE_BASENAME)
	sudo rm -fr /usr/share/$(MODULE_NAME)

clean:
	-rm -rf build