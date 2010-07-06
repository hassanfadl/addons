# -*- encoding: utf-8 -*-
##############################################################################
#
#    OpenERP, Open Source Management Solution
#
#    Copyright (C) P. Christeas, 2009, all rights reserved
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

from osv import osv, fields
import os
import tools
import base64
import errno
import logging
from StringIO import StringIO

from tools.misc import ustr
from tools.translate import _

from osv.orm import except_orm

import random
import string
import pooler
import netsvc
import nodes
from content_index import cntIndex

DMS_ROOT_PATH = tools.config.get('document_path', os.path.join(tools.config.get('root_path'), 'filestore'))


""" The algorithm of data storage

We have to consider 3 cases of data /retrieval/:
 Given (context,path) we need to access the file (aka. node).
 given (directory, context), we need one of its children (for listings, views)
 given (ir.attachment, context), we needs its data and metadata (node).

For data /storage/ we have the cases:
 Have (ir.attachment, context), we modify the file (save, update, rename etc).
 Have (directory, context), we create a file.
 Have (path, context), we create or modify a file.
 
Note that in all above cases, we don't explicitly choose the storage media,
but always require a context to be present.

Note that a node will not always have a corresponding ir.attachment. Dynamic
nodes, for once, won't. Their metadata will be computed by the parent storage
media + directory.

The algorithm says that in any of the above cases, our first goal is to locate
the node for any combination of search criteria. It would be wise NOT to 
represent each node in the path (like node[/] + node[/dir1] + node[/dir1/dir2])
but directly jump to the end node (like node[/dir1/dir2]) whenever possible.

We also contain all the parenting loop code in one function. This is intentional,
because one day this will be optimized in the db (Pg 8.4).


"""

def random_name():
    random.seed()
    d = [random.choice(string.ascii_letters) for x in xrange(10) ]
    name = "".join(d)
    return name

INVALID_CHARS = {'*':str(hash('*')), '|':str(hash('|')) , "\\":str(hash("\\")), '/':'__', ':':str(hash(':')), '"':str(hash('"')), '<':str(hash('<')) , '>':str(hash('>')) , '?':str(hash('?'))}


def create_directory(path):
    dir_name = random_name()
    path = os.path.join(path, dir_name)
    os.makedirs(path)
    return dir_name

class nodefd_file(nodes.node_descriptor):
    """ A descriptor to a real file

    Inheriting directly from file doesn't work, since file exports
    some read-only attributes (like 'name') that we don't like.
    """
    def __init__(self, parent, path, mode):
        nodes.node_descriptor.__init__(self, parent)
        self.__file = open(path, mode)
        
        for attr in ('closed', 'read', 'write', 'seek', 'tell'):
            setattr(self,attr, getattr(self.__file, attr))

    def close(self):
        # TODO: locking in init, close()
        self.__file.close()

    
class nodefd_db(StringIO, nodes.node_descriptor):
    """ A descriptor to db data
    """
    def __init__(self, parent, ira_browse, mode):
        nodes.node_descriptor.__init__(self, parent)
        if mode.endswith('b'):
            mode = mode[:-1]
        
        if mode in ('r', 'r+'):
            cr.execute('SELECT db_datas FROM ir_attachment WHERE id = %s', ira_browse.id)
            data = cr.fetchone()[0]
            StringIO.__init__(self, data)
        elif mode in ('w', 'w+'):
            StringIO.__init__(self, None)
            # at write, we start at 0 (= overwrite), but have the original
            # data available, in case of a seek()
        elif mode == 'a':
            StringIO.__init__(self, None)
        else:
            logging.getLogger('document.storage').error("Incorrect mode %s specified", mode)
            raise IOError(errno.EINVAL, "Invalid file mode")
        self.mode = mode

    def close(self):
        # we now open a *separate* cursor, to update the data.
        # FIXME: this may be improved, for concurrency handling
        par = self._get_parent()
        uid = par.context.uid
        cr = pooler.get_db(par.context.dbname).cursor()
        try:
            if self.mode in ('w', 'w+', 'r+'):
                out = self.getvalue()
                cr.execute("UPDATE ir_attachment SET db_datas = decode(%s,'escape'), file_size=%s WHERE id = %s",
                    (out, len(out), par.file_id))
            elif self.mode == 'a':
                out = self.getvalue()
                cr.execute("UPDATE ir_attachment " \
                    "SET db_datas = COALESCE(db_datas,'') || decode(%s, 'escape'), " \
                    "    file_size = COALESCE(file_size, 0) + %s " \
                    " WHERE id = %s",
                    (out, len(out), par.file_id))
            cr.commit()
        except Exception, e:
            logging.getLogger('document.storage').exception('Cannot update db file #%d for close:', par.file_id)
            raise
        finally:
            cr.close()
        StringIO.close(self)

class nodefd_db64(StringIO, nodes.node_descriptor):
    """ A descriptor to db data, base64 (the old way)
    
        It stores the data in base64 encoding at the db. Not optimal, but
        the transparent compression of Postgres will save the day.
    """
    def __init__(self, parent, ira_browse, mode):
        nodes.node_descriptor.__init__(self, parent)
        if mode.endswith('b'):
            mode = mode[:-1]
        
        if mode in ('r', 'r+'):
            StringIO.__init__(self, base64.decodestring(ira_browse.db_datas))
        elif mode in ('w', 'w+'):
            StringIO.__init__(self, None)
            # at write, we start at 0 (= overwrite), but have the original
            # data available, in case of a seek()
        elif mode == 'a':
            StringIO.__init__(self, None)
        else:
            logging.getLogger('document.storage').error("Incorrect mode %s specified", mode)
            raise IOError(errno.EINVAL, "Invalid file mode")
        self.mode = mode

    def close(self):
        # we now open a *separate* cursor, to update the data.
        # FIXME: this may be improved, for concurrency handling
        par = self._get_parent()
        uid = par.context.uid
        cr = pooler.get_db(par.context.dbname).cursor()
        try:
            if self.mode in ('w', 'w+', 'r+'):
                out = self.getvalue()
                cr.execute('UPDATE ir_attachment SET db_datas = %s::bytea, file_size=%s WHERE id = %s',
                    (base64.encodestring(out), len(out), par.file_id))
            elif self.mode == 'a':
                out = self.getvalue()
                # Yes, we're obviously using the wrong representation for storing our
                # data as base64-in-bytea
                cr.execute("UPDATE ir_attachment " \
                    "SET db_datas = encode( (COALESCE(decode(encode(db_datas,'escape'),'base64'),'') || decode(%s, 'base64')),'base64')::bytea , " \
                    "    file_size = COALESCE(file_size, 0) + %s " \
                    " WHERE id = %s",
                    (base64.encodestring(out), len(out), par.file_id))
            cr.commit()
        except Exception, e:
            logging.getLogger('document.storage').exception('Cannot update db file #%d for close:', par.file_id)
            raise
        finally:
            cr.close()
        StringIO.close(self)

class document_storage(osv.osv):
    """ The primary object for data storage.
    Each instance of this object is a storage media, in which our application
    can store contents. The object here controls the behaviour of the storage
    media.
    The referring document.directory-ies will control the placement of data
    into the storage.
    
    It is a bad idea to have multiple document.storage objects pointing to
    the same tree of filesystem storage.
    """
    _name = 'document.storage'
    _description = 'Storage Media'
    _doclog = logging.getLogger('document')

    _columns = {
        'name': fields.char('Name', size=64, required=True, select=1),
        'write_date': fields.datetime('Date Modified', readonly=True),
        'write_uid':  fields.many2one('res.users', 'Last Modification User', readonly=True),
        'create_date': fields.datetime('Date Created', readonly=True),
        'create_uid':  fields.many2one('res.users', 'Creator', readonly=True),
        'user_id': fields.many2one('res.users', 'Owner'),
        'group_ids': fields.many2many('res.groups', 'document_storage_group_rel', 'item_id', 'group_id', 'Groups'),
        'dir_ids': fields.one2many('document.directory', 'parent_id', 'Directories'),
        'type': fields.selection([('db', 'Database'), ('filestore', 'Internal File storage'),
            ('realstore', 'External file storage'), ('virtual', 'Virtual storage')], 'Type', required=True),
        'path': fields.char('Path', size=250, select=1, help="For file storage, the root path of the storage"),
        'online': fields.boolean('Online', help="If not checked, media is currently offline and its contents not available", required=True),
        'readonly': fields.boolean('Read Only', help="If set, media is for reading only"),
    }

    def _get_rootpath(self, cr, uid, context=None):
        return os.path.join(DMS_ROOT_PATH, cr.dbname)

    _defaults = {
        'user_id': lambda self, cr, uid, ctx: uid,
        'online': lambda *args: True,
        'readonly': lambda *args: False,
        # Note: the defaults below should only be used ONCE for the default
        # storage media. All other times, we should create different paths at least.
        'type': lambda *args: 'filestore',
        'path': _get_rootpath,
    }
    _sql_constraints = [
        # SQL note: a path = NULL doesn't have to be unique.
        ('path_uniq', 'UNIQUE(type,path)', "The storage path must be unique!")
        ]

    def get_data(self, cr, uid, id, file_node, context=None, fil_obj=None):
        """ retrieve the contents of some file_node having storage_id = id
            optionally, fil_obj could point to the browse object of the file
            (ir.attachment)
        """
        if not context:
            context = {}
        boo = self.browse(cr, uid, id, context)
        if fil_obj:
            ira = fil_obj
        else:
            ira = self.pool.get('ir.attachment').browse(cr, uid, file_node.file_id, context=context)
        return self.__get_data_3(cr, uid, boo, ira, context)

    def get_file(self, cr, uid, id, file_node, mode, context=None):
        if context is None:
            context = {}
        boo = self.browse(cr, uid, id, context)
        if not boo.online:
            raise RuntimeError('media offline')
        
        ira = self.pool.get('ir.attachment').browse(cr, uid, file_node.file_id, context=context)
        if boo.type == 'filestore':
            if not ira.store_fname:
                # On a migrated db, some files may have the wrong storage type
                # try to fix their directory.
                if ira.file_size:
                    self._doclog.warning( "ir.attachment #%d does not have a filename, but is at filestore, fix it!" % ira.id)
                raise IOError(errno.ENOENT, 'No file can be located')
            fpath = os.path.join(boo.path, ira.store_fname)
            return nodefd_file(file_node, path=fpath, mode=mode)

        elif boo.type == 'db':
            # TODO: we need a better api for large files
            return nodefd_db64(file_node, ira_browse=ira, mode=mode)

        elif boo.type == 'realstore':
            if not ira.store_fname:
                # On a migrated db, some files may have the wrong storage type
                # try to fix their directory.
                if ira.file_size:
                    self._doclog.warning("ir.attachment #%d does not have a filename, trying the name." %ira.id)
                sfname = ira.name
            fpath = os.path.join(boo.path,ira.store_fname or ira.name)
            if not os.path.exists(fpath):
                raise IOError("File not found: %s" % fpath)
            return nodefd_file(file_node, path=fpath, mode=mode)

        else:
            raise TypeError("No %s storage" % boo.type)

    def __get_data_3(self, cr, uid, boo, ira, context):
        if not boo.online:
            raise RuntimeError('media offline')
        if boo.type == 'filestore':
            if not ira.store_fname:
                # On a migrated db, some files may have the wrong storage type
                # try to fix their directory.
                if ira.file_size:
                    self._doclog.warning( "ir.attachment #%d does not have a filename, but is at filestore, fix it!" % ira.id)
                return None
            fpath = os.path.join(boo.path, ira.store_fname)
            return file(fpath, 'rb').read()
        elif boo.type == 'db':
            # TODO: we need a better api for large files
            if ira.db_datas:
                out = base64.decodestring(ira.db_datas)
            else:
                out = ''
            return out
        elif boo.type == 'realstore':
            if not ira.store_fname:
                # On a migrated db, some files may have the wrong storage type
                # try to fix their directory.
                if ira.file_size:
                    self._doclog.warning("ir.attachment #%d does not have a filename, trying the name." %ira.id)
                sfname = ira.name
            fpath = os.path.join(boo.path,ira.store_fname or ira.name)
            if os.path.exists(fpath):
                return file(fpath,'rb').read()
            elif not ira.store_fname:
                return None
            else:
                raise IOError("File not found: %s" % fpath)
        else:
            raise TypeError("No %s storage" % boo.type)

    def set_data(self, cr, uid, id, file_node, data, context=None, fil_obj=None):
        """ store the data.
            This function MUST be used from an ir.attachment. It wouldn't make sense
            to store things persistently for other types (dynamic).
        """
        if not context:
            context = {}
        boo = self.browse(cr, uid, id, context)
        if fil_obj:
            ira = fil_obj
        else:
            ira = self.pool.get('ir.attachment').browse(cr, uid, file_node.file_id, context=context)

        if not boo.online:
            raise RuntimeError('media offline')
        self._doclog.debug( "Store data for ir.attachment #%d" % ira.id)
        store_fname = None
        fname = None
        if boo.type == 'filestore':
            path = boo.path
            try:
                flag = None
                # This can be improved  
                if os.path.isdir(path):
                    for dirs in os.listdir(path):
                        if os.path.isdir(os.path.join(path, dirs)) and len(os.listdir(os.path.join(path, dirs))) < 4000:
                            flag = dirs
                            break
                flag = flag or create_directory(path)
                filename = random_name()
                fname = os.path.join(path, flag, filename)
                fp = file(fname, 'wb')
                fp.write(data)
                fp.close()
                self._doclog.debug( "Saved data to %s" % fname)
                filesize = len(data) # os.stat(fname).st_size
                store_fname = os.path.join(flag, filename)

                # TODO Here, an old file would be left hanging.

            except Exception, e:
                self._doclog.warning( "Couldn't save data to %s", path, exc_info=True)
                raise except_orm(_('Error!'), str(e))
        elif boo.type == 'db':
            filesize = len(data)
            # will that work for huge data? TODO
            out = base64.encodestring(data)
            cr.execute('UPDATE ir_attachment SET db_datas = %s WHERE id = %s',
                (out, file_node.file_id))
        elif boo.type == 'realstore':
            try:
                file_node.fix_ppath(cr, ira)
                npath = file_node.full_path() or []
                # npath may contain empty elements, for root directory etc.
                for i, n in enumerate(npath):
                    if n == None:
                        del npath[i]
                for n in npath:
                    for ch in ('*', '|', "\\", '/', ':', '"', '<', '>', '?', '..'):
                        if ch in n:
                            raise ValueError("Invalid char %s in path %s" %(ch, n))
                dpath = [boo.path,]
                dpath += npath[:-1]
                path = os.path.join(*dpath)
                if not os.path.isdir(path):
                    os.makedirs(path)
                fname = os.path.join(path, npath[-1])
                fp = file(fname,'wb')
                fp.write(data)
                fp.close()
                self._doclog.debug("Saved data to %s", fname)
                filesize = len(data) # os.stat(fname).st_size
                store_fname = os.path.join(*npath)
                # TODO Here, an old file would be left hanging.
            except Exception,e :
                self._doclog.warning("Couldn't save data:", exc_info=True)
                raise except_orm(_('Error!'), str(e))
        else:
            raise TypeError("No %s storage" % boo.type)

        # 2nd phase: store the metadata
        try:
            icont = ''
            mime = ira.file_type
            if not mime:
                mime = ""
            try:
                mime, icont = cntIndex.doIndex(data, ira.datas_fname,
                ira.file_type or None, fname)
            except Exception:
                self._doclog.debug('Cannot index file:', exc_info=True)
                pass

            try:
                icont_u = ustr(icont)
            except UnicodeError:
                icont_u = ''

            # a hack: /assume/ that the calling write operation will not try
            # to write the fname and size, and update them in the db concurrently.
            # We cannot use a write() here, because we are already in one.
            cr.execute('UPDATE ir_attachment SET store_fname = %s, file_size = %s, index_content = %s, file_type = %s WHERE id = %s',
                (store_fname, filesize, icont_u, mime, file_node.file_id))
            file_node.content_length = filesize
            file_node.content_type = mime
            return True
        except Exception, e :
            self._doclog.warning("Couldn't save data:", exc_info=True)
            # should we really rollback once we have written the actual data?
            # at the db case (only), that rollback would be safe
            raise except_orm(_('Error at doc write!'), str(e))

    def prepare_unlink(self, cr, uid, storage_bo, fil_bo):
        """ Before we unlink a file (fil_boo), prepare the list of real
        files that have to be removed, too. """

        if not storage_bo.online:
            raise RuntimeError('media offline')

        if storage_bo.type == 'filestore':
            fname = fil_bo.store_fname
            if not fname:
                return None
            path = storage_bo.path
            return (storage_bo.id, 'file', os.path.join(path, fname))
        elif storage_bo.type == 'db':
            return None
        elif storage_bo.type == 'realstore':
            fname = fil_bo.store_fname
            if not fname:
                return None
            path = storage_bo.path
            return ( storage_bo.id, 'file', os.path.join(path, fname))
        else:
            raise TypeError("No %s storage" % storage_bo.type)

    def do_unlink(self, cr, uid, unres):
        for id, ktype, fname in unres:
            if ktype == 'file':
                try:
                    os.unlink(fname)
                except Exception, e:
                    self._doclog.warning("Could not remove file %s, please remove manually.", fname, exc_info=True)
            else:
                self._doclog.warning("Unknown unlink key %s" % ktype)

        return True

    def simple_rename(self, cr, uid, file_node, new_name, context=None):
        """ A preparation for a file rename.
            It will not affect the database, but merely check and perhaps
            rename the realstore file.
            
            @return the dict of values that can safely be be stored in the db.
        """
        sbro = self.browse(cr, uid, file_node.storage_id, context=context)
        assert sbro, "The file #%d didn't provide storage" % file_node.file_id
        
        if sbro.type in ('filestore', 'db'):
            # nothing to do for a rename, allow to change the db field
            return { 'name': new_name, 'datas_fname': new_name }
        elif sbro.type == 'realstore':
            fname = fil_bo.store_fname
            if not fname:
                return ValueError("Tried to rename a non-stored file")
            path = storage_bo.path
            oldpath = os.path.join(path, fname)
            
            for ch in ('*', '|', "\\", '/', ':', '"', '<', '>', '?', '..'):
                if ch in new_name:
                    raise ValueError("Invalid char %s in name %s" %(ch, new_name))
                
            file_node.fix_ppath(cr, ira)
            npath = file_node.full_path() or []
            dpath = [path,]
            dpath.extend(npath[:-1])
            dpath.append(new_name)
            newpath = os.path.join(*dpath)
            # print "old, new paths:", oldpath, newpath
            os.rename(oldpath, newpath)
            return { 'name': new_name, 'datas_fname': new_name, 'store_fname': new_name }
        else:
            raise TypeError("No %s storage" % boo.type)


document_storage()


#eof
