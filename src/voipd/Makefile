# voipd Makefile
# 某些发行版的 mediastreamer.pc 引用了不存在的 -lmediastreamer_base/_voip，
# 因此不直接使用 pkg-config --libs；liblinphone 4.4 和 5.x 的库名不同。

CXX      ?= g++
CXXFLAGS ?= -O2 -g -Wall -Wextra -Wno-deprecated-declarations -std=c++17
GLIB_CFLAGS := $(shell pkg-config --cflags glib-2.0 gio-2.0)
GLIB_LIBS   := $(shell pkg-config --libs glib-2.0 gio-2.0)
MEDIA_LIB := $(if $(filter libmediastreamer2.so,$(shell $(CXX) -print-file-name=libmediastreamer2.so)),-lmediastreamer,-lmediastreamer2)
BELLE_LIB := $(if $(filter libbelle-sip.so,$(shell $(CXX) -print-file-name=libbelle-sip.so)),-lbellesip,-lbelle-sip)
LINPHONE_LIBS ?= -llinphone $(MEDIA_LIB) -lortp -lbelcard -lbctoolbox $(BELLE_LIB)
LINPHONE_CFLAGS := -I/usr/include

# 4.4 的非系统资源目录需要 belr 搜索路径；有开发包时自动启用。
# 可显式传 VOIP_WITH_BELR=0/1 覆盖；5.x 系统安装默认不增加该依赖。
VOIP_WITH_BELR ?= $(if $(wildcard /usr/include/linphone/api/c-account-params.h),0,$(if $(wildcard /usr/include/belr/grammarbuilder.h),1,0))
ifeq ($(VOIP_WITH_BELR),1)
LINPHONE_CFLAGS += -DVOIP_WITH_BELR
LINPHONE_LIBS += -lbelr
endif

PREFIX ?= /usr/local
BINDIR := $(PREFIX)/bin

TARGET := ../../build/voipd

all: $(TARGET)

$(TARGET): voipd.cpp Makefile
	mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS) $(GLIB_CFLAGS) $(LINPHONE_CFLAGS) -o $@ $< $(LINPHONE_LIBS) $(GLIB_LIBS)

install: $(TARGET)
	install -d $(DESTDIR)$(BINDIR)
	install -m 0755 $(TARGET) $(DESTDIR)$(BINDIR)/$(TARGET)

clean:
	rm -f $(TARGET)

.PHONY: all install clean
